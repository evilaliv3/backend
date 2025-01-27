"""
Measurements API
The routes are mounted under /api
"""

from datetime import datetime, timedelta
from dateutil.parser import parse as parse_date
from pathlib import Path
from typing import Optional, Any, Dict
import gzip
import json
import logging
import math
import time

import ujson  # debdeps: python3-ujson
import urllib3  # debdeps: python3-urllib3

from flask import current_app, request, make_response, abort, redirect, Response
from flask.json import jsonify
from werkzeug.exceptions import HTTPException, BadRequest

# debdeps: python3-sqlalchemy
from sqlalchemy import and_, text, select, sql, column
from sqlalchemy.exc import OperationalError
from psycopg2.extensions import QueryCanceledError  # debdeps: python3-psycopg2

from urllib.request import urlopen
from urllib.parse import urljoin, urlencode

from ooniapi.auth import role_required, get_account_id_or_none
from ooniapi.config import metrics
from ooniapi.utils import cachedjson, nocachejson, jerror
from ooniapi.database import query_click, query_click_one_row
from ooniapi.urlparams import (
    param_asn,
    param_bool,
    param_commasplit,
    param_date,
    param_input_or_none,
    param_report_id,
    param_report_id_or_none,
    param_measurement_uid,
)

from flask import Blueprint

api_msm_blueprint = Blueprint("msm_api", "measurements")

FASTPATH_MSM_ID_PREFIX = "temp-fid-"
FASTPATH_SERVER = "fastpath.ooni.nu"
FASTPATH_PORT = 8000

log = logging.getLogger()

urllib_pool = urllib3.PoolManager()

# type hints
ostr = Optional[str]


class QueryTimeoutError(HTTPException):
    code = 504
    description = "The database query timed out.\nTry changing the query parameters."


class MsmtNotFound(HTTPException):
    code = 500
    description = "Measurement not found"


@api_msm_blueprint.route("/")
def show_apidocs():
    """Route to https://api.ooni.io/api/ to /apidocs/"""
    return redirect("/apidocs")


@api_msm_blueprint.route("/v1/files")
def list_files() -> Response:
    """List files - unsupported"""
    return cachedjson("1d", msg="not implemented")


def measurement_uid_to_s3path_linenum(measurement_uid: str):
    # TODO: cleanup this
    query = """SELECT s3path, linenum FROM jsonl
        PREWHERE (report_id, input) IN (
            SELECT report_id, input FROM fastpath WHERE measurement_uid = :uid
        )
        LIMIT 1"""
    query_params = dict(uid=measurement_uid)
    lookup = query_click_one_row(sql.text(query), query_params, query_prio=3)
    if lookup is None:
        raise MsmtNotFound

    s3path = lookup["s3path"]
    linenum = lookup["linenum"]
    return s3path, linenum


@metrics.timer("get_measurement")
@api_msm_blueprint.route("/v1/measurement/<measurement_uid>")
def get_measurement(measurement_uid) -> Response:
    """Get one measurement by measurement_id,
    Returns only the measurement without extra data from the database
    ---
    parameters:
      - name: measurement_uid
        in: path
        required: true
        type: string
      - name: download
        in: query
        type: boolean
        description: triggers a file download
    responses:
      '200':
        description: Returns the JSON blob for the specified measurement
    """
    log = current_app.logger
    assert measurement_uid
    param = request.args.get
    download = param("download", "").lower() == "true"
    try:
        s3path, linenum = measurement_uid_to_s3path_linenum(measurement_uid)
    except MsmtNotFound:
        return jerror("Incorrect or inexistent measurement_uid")

    log.debug(f"Fetching file {s3path} from S3")
    try:
        body = _fetch_jsonl_measurement_body_from_s3(s3path, linenum)
    except Exception:  # pragma: no cover
        log.error(f"Failed to fetch file {s3path} from S3")
        return jerror("Incorrect or inexistent measurement_uid")

    resp = make_response(body)
    resp.mimetype = "application/json"
    resp.cache_control.max_age = 3600
    if download:
        set_dload(resp, f"ooni_measurement-{measurement_uid}.json")

    return resp


# # Fetching measurement bodies


@metrics.timer("_fetch_jsonl_measurement_body_from_s3")
def _fetch_jsonl_measurement_body_from_s3(
    s3path: str,
    linenum: int,
) -> bytes:
    log = current_app.logger
    bucket_name = current_app.config["S3_BUCKET_NAME"]
    baseurl = f"https://{bucket_name}.s3.amazonaws.com/"
    url = urljoin(baseurl, s3path)
    log.info(f"Fetching {url}")
    r = urlopen(url)
    f = gzip.GzipFile(fileobj=r, mode="r")
    for n, line in enumerate(f):
        if n == linenum:
            return line

    raise MsmtNotFound


def report_id_input_to_s3path_linenum(report_id: str, input: str):
    query = """SELECT s3path, linenum FROM jsonl
        PREWHERE report_id = :report_id AND input = :inp
        LIMIT 1"""
    query_params = dict(inp=input, report_id=report_id)
    lookup = query_click_one_row(sql.text(query), query_params, query_prio=3)

    if lookup is None:
        m = f"Missing row in jsonl table: {report_id} {input}"
        log.error(m)
        metrics.incr("msmt_not_found_in_jsonl")
        raise MsmtNotFound

    s3path = lookup["s3path"]
    linenum = lookup["linenum"]
    return s3path, linenum


@metrics.timer("_fetch_jsonl_measurement_body_clickhouse")
def _fetch_jsonl_measurement_body_clickhouse(
    report_id: str, input: Optional[str], measurement_uid: Optional[str]
) -> Optional[bytes]:
    """
    Fetch jsonl from S3, decompress it, extract single msmt
    """
    # TODO: switch to _fetch_measurement_body_by_uid
    if measurement_uid is not None:
        try:
            s3path, linenum = measurement_uid_to_s3path_linenum(measurement_uid)
        except MsmtNotFound:
            log.error(f"Measurement {measurement_uid} not found in jsonl")
            return None

    else:
        try:
            inp = input or ""  # NULL/None input is stored as ''
            s3path, linenum = report_id_input_to_s3path_linenum(report_id, inp)
        except Exception:
            log.error(f"Measurement {report_id} {inp} not found in jsonl")
            return None

    try:
        log.debug(f"Fetching file {s3path} from S3")
        return _fetch_jsonl_measurement_body_from_s3(s3path, linenum)
    except Exception:  # pragma: no cover
        log.error(f"Failed to fetch file {s3path} from S3")
        return None


def _unwrap_post(post: dict) -> dict:
    fmt = post.get("format", "")
    if fmt == "json":
        return post.get("content", {})
    raise Exception("Unexpected format")


@metrics.timer("_fetch_measurement_body_on_disk_by_msmt_uid")
def _fetch_measurement_body_on_disk_by_msmt_uid(msmt_uid: str) -> Optional[bytes]:
    """Fetch raw POST from disk, extract msmt
    This is used only for msmts that have been processed by the fastpath
    but are not uploaded to S3 yet.
    YAML msmts not supported: requires implementing normalization here
    """
    assert msmt_uid.startswith("20")
    tstamp, cc, testname, hash_ = msmt_uid.split("_")
    hour = tstamp[:10]
    int(hour)  # raise if the string does not contain an integer
    spooldir = Path("/var/lib/ooniapi/measurements/incoming/")
    postf = spooldir / f"{hour}_{cc}_{testname}/{msmt_uid}.post"
    log.debug(f"Attempt at reading {postf}")
    try:
        with postf.open() as f:
            post = ujson.load(f)
    except FileNotFoundError:
        return None
    body = _unwrap_post(post)
    return ujson.dumps(body).encode()


def _fetch_measurement_body_by_uid(msmt_uid: str) -> bytes:
    """Fetch measurement body from either disk or jsonl on S3"""
    log.debug(f"Fetching body for UID {msmt_uid}")
    body = _fetch_measurement_body_on_disk_by_msmt_uid(msmt_uid)
    if body is not None:
        return body

    log.debug(f"Fetching body for UID {msmt_uid} from jsonl on S3")
    s3path, linenum = measurement_uid_to_s3path_linenum(msmt_uid)
    return _fetch_jsonl_measurement_body_from_s3(s3path, linenum)


@metrics.timer("_fetch_measurement_body_from_hosts")
def _fetch_measurement_body_from_hosts(msmt_uid: str) -> Optional[bytes]:
    """Fetch raw POST from another API host, extract msmt
    This is used only for msmts that have been processed by the fastpath
    but are not uploaded to S3 yet.
    """
    try:
        assert msmt_uid.startswith("20")
        tstamp, cc, testname, hash_ = msmt_uid.split("_")
        hour = tstamp[:10]
        int(hour)
        path = f"{hour}_{cc}_{testname}/{msmt_uid}.post"
    except Exception:
        log.info("Error", exc_info=True)
        return None

    for hostname in current_app.config["OTHER_COLLECTORS"]:
        url = urljoin(f"https://{hostname}/measurement_spool/", path)
        log.debug(f"Attempt to load {url}")
        try:
            r = urllib_pool.request("GET", url)
            if r.status == 404:
                log.debug("not found")
                continue
            elif r.status != 200:
                log.error(f"unexpected status {r.status}")
                continue

            post = ujson.loads(r.data)
            body = _unwrap_post(post)
            return ujson.dumps(body).encode()
        except Exception:
            log.info("Error", exc_info=True)
            pass

    return None


@metrics.timer("fetch_measurement_body")
def _fetch_measurement_body(
    report_id: str, input: Optional[str], measurement_uid: str
) -> bytes:
    """Fetch measurement body from either:
    - local measurement spool dir (.post files)
    - JSONL files on S3
    - remote measurement spool dir (another API/collector host)
    """
    # TODO: uid_cleanup
    log.debug(f"Fetching body for {report_id} {input}")
    u_count = report_id.count("_")
    # 5: Current format e.g.
    # 20210124T210009Z_webconnectivity_VE_22313_n1_Ojb<redacted>
    new_format = u_count == 5 and measurement_uid

    if new_format:
        ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y%m%d%H%M")
        fresh = measurement_uid > ts

    # Do the fetching in different orders based on the likelyhood of success
    if new_format and fresh:
        body = (
            _fetch_measurement_body_on_disk_by_msmt_uid(measurement_uid)
            or _fetch_measurement_body_from_hosts(measurement_uid)
            or _fetch_jsonl_measurement_body_clickhouse(
                report_id, input, measurement_uid
            )
        )

    elif new_format and not fresh:
        body = (
            _fetch_jsonl_measurement_body_clickhouse(report_id, input, measurement_uid)
            or _fetch_measurement_body_on_disk_by_msmt_uid(measurement_uid)
            or _fetch_measurement_body_from_hosts(measurement_uid)
        )

    else:
        body = _fetch_jsonl_measurement_body_clickhouse(
            report_id, input, measurement_uid
        )

    if body:
        metrics.incr("msmt_body_found")
        return body

    metrics.incr("msmt_body_not_found")
    raise MsmtNotFound


def genurl(path: str, **kw) -> str:
    """Generate absolute URL for the API"""
    base = current_app.config["BASE_URL"]
    return urljoin(base, path) + "?" + urlencode(kw)


@api_msm_blueprint.route("/v1/raw_measurement")
@metrics.timer("get_raw_measurement")
def get_raw_measurement() -> Response:
    """Get raw measurement body by report_id + input
    ---
    parameters:
      - name: report_id
        in: query
        type: string
        description: The report_id to search measurements for
      - name: input
        in: query
        type: string
        minLength: 3
        description: The input (for example a URL or IP address) to search measurements for
      - name: measurement_uid
        in: query
        type: string
        description: The measurement_uid to search measurements for
    responses:
      '200':
        description: raw measurement body, served as JSON file to be dowloaded
    """
    # This is used by Explorer to let users download msmts
    try:
        msmt_uid = param_measurement_uid()
        # TODO: uid_cleanup
        msmt_meta = _get_measurement_meta_by_uid(msmt_uid)
    except Exception:
        report_id = param_report_id()
        param = request.args.get
        input_ = param("input")
        # _fetch_measurement_body needs the UID
        msmt_meta = _get_measurement_meta_clickhouse(report_id, input_)

    if msmt_meta:
        body = _fetch_measurement_body(
            msmt_meta["report_id"], msmt_meta["input"], msmt_meta["measurement_uid"]
        )
        resp = make_response(body)
    else:
        resp = make_response({})

    resp.headers.set("Content-Type", "application/json")
    resp.cache_control.max_age = 24 * 3600
    return resp




def format_msmt_meta(msmt_meta: dict) -> dict:
    keys = (
        "input",
        "measurement_start_time",
        "measurement_uid",
        "report_id",
        "test_name",
        "test_start_time",
        "probe_asn",
        "probe_cc",
        "scores",
    )
    out = {k: msmt_meta[k] for k in keys}
    out["category_code"] = msmt_meta.get("category_code", None)
    out["anomaly"] = msmt_meta["anomaly"] == "t"
    out["confirmed"] = msmt_meta["confirmed"] == "t"
    out["failure"] = msmt_meta["msm_failure"] == "t"
    return out


@metrics.timer("get_measurement_meta_clickhouse")
def _get_measurement_meta_clickhouse(report_id: str, input_: Optional[str]) -> dict:
    # Given report_id + input, fetch measurement data from fastpath table
    query = "SELECT * FROM fastpath "
    if input_ is None:
        # fastpath uses input = '' for empty values
        query += "WHERE report_id = :report_id AND input = '' "
    else:
        # Join citizenlab to return category_code (useful only for web conn)
        query += """
        LEFT OUTER JOIN citizenlab ON citizenlab.url = fastpath.input
        WHERE fastpath.input = :input
        AND fastpath.report_id = :report_id
        """
    query_params = dict(input=input_, report_id=report_id)
    query += "LIMIT 1"
    msmt_meta = query_click_one_row(sql.text(query), query_params, query_prio=3)
    if not msmt_meta:
        return {}  # measurement not found
    if msmt_meta["probe_asn"] == 0:
        # https://ooni.org/post/2020-ooni-probe-asn-incident-report/
        # https://github.com/ooni/explorer/issues/495
        return {}  # unwanted

    return format_msmt_meta(msmt_meta)


@metrics.timer("get_measurement_meta_by_uid")
def _get_measurement_meta_by_uid(measurement_uid: str) -> dict:
    query = """SELECT * FROM fastpath
        LEFT OUTER JOIN citizenlab ON citizenlab.url = fastpath.input
        WHERE measurement_uid = :uid
        LIMIT 1
    """
    query_params = dict(uid=measurement_uid)
    msmt_meta = query_click_one_row(sql.text(query), query_params, query_prio=3)
    if not msmt_meta:
        return {}  # measurement not found
    if msmt_meta["probe_asn"] == 0:
        # https://ooni.org/post/2020-ooni-probe-asn-incident-report/
        # https://github.com/ooni/explorer/issues/495
        return {}  # unwanted

    return format_msmt_meta(msmt_meta)


@api_msm_blueprint.route("/v1/measurement_meta")
@metrics.timer("get_measurement_meta")
def get_measurement_meta() -> Response:
    """Get metadata on one measurement by measurement_uid or report_id + input
    ---
    produces:
      - application/json
    parameters:
      - name: measurement_uid
        in: query
        type: string
        description: The measurement ID, mutually exclusive with report_id + input
      - name: report_id
        in: query
        type: string
        description: The report_id to search measurements for
        example: 20210208T162755Z_ndt_DZ_36947_n1_8swgXi7xNuRUyO9a
      - name: input
        in: query
        type: string
        description: The input (for example a URL or IP address) to search measurements for
      - name: full
        in: query
        type: boolean
        description: Include JSON measurement data
    responses:
      200:
        description: Returns measurement metadata, optionally including the raw measurement body
        schema:
          type: object
          properties:
            anomaly:
              type: boolean
            category_code:
              type: string
            confirmed:
              type: boolean
            failure:
              type: boolean
            input:
              type: string
            measurement_start_time:
              type: string
            probe_asn:
              type: integer
            probe_cc:
              type: string
            raw_measurement:
              type: string
            report_id:
              type: string
            scores:
              type: string
            test_name:
              type: string
            test_start_time:
              type: string
          example: {
            "anomaly": false,
            "confirmed": false,
            "failure": false,
            "input": null,
            "measurement_start_time": "2021-02-08T23:31:46Z",
            "probe_asn": 36947,
            "probe_cc": "DZ",
            "report_id": "20210208T162755Z_ndt_DZ_36947_n1_8swgXi7xNuRUyO9a",
            "scores": "{}",
            "test_name": "ndt",
            "test_start_time": "2021-02-08T23:31:43Z"
          }
    """

    # TODO: input can be '' or NULL in the fastpath table - fix it
    # TODO: see integ tests for TODO items
    param = request.args.get
    full = param("full", "").lower() in ("true", "1", "yes")
    try:
        msmt_uid = param_measurement_uid()
        log.info(f"get_measurement_meta {msmt_uid}")
        msmt_meta = _get_measurement_meta_by_uid(msmt_uid)
    except Exception:
        report_id = param_report_id()
        input_ = param_input_or_none()
        log.info(f"get_measurement_meta {report_id} {input_}")
        msmt_meta = _get_measurement_meta_clickhouse(report_id, input_)

    assert isinstance(msmt_meta, dict)
    if not full:
        return cachedjson("1m", **msmt_meta)

    if msmt_meta == {}:  # measurement not found
        return cachedjson("1m", raw_measurement="", **msmt_meta)

    try:
        # TODO: uid_cleanup
        body = _fetch_measurement_body(
            msmt_meta["report_id"], msmt_meta["input"], msmt_meta["measurement_uid"]
        )
        assert isinstance(body, bytes)
        body = body.decode()
    except Exception as e:
        log.error(e, exc_info=True)
        body = ""

    return cachedjson("1m", raw_measurement=body, **msmt_meta)


# # Listing measurements


@api_msm_blueprint.route("/v1/measurements")
@metrics.timer("list_measurements")
def list_measurements() -> Response:
    """Search for measurements using only the database. Provide pagination.
    ---
    parameters:
      - name: report_id
        in: query
        type: string
        description: Report_id to search measurements for
      - name: input
        in: query
        type: string
        minLength: 3 # `input` is handled by pg_trgm
        description: Input (for example a URL or IP address) to search measurements for
      - name: domain
        in: query
        type: string
        minLength: 3
        description: Domain to search measurements for
      - name: probe_cc
        in: query
        type: string
        description: Two letter country code
      - name: probe_asn
        in: query
        type: string
        description: Autonomous system number in the format "ASXXX"
      - name: test_name
        in: query
        type: string
        description: Name of the test
      - name: category_code
        in: query
        type: string
        description: Category code from the citizenlab list
      - name: since
        in: query
        type: string
        description: >-
          Start date of when measurements were run (ex.
          "2016-10-20T10:30:00")
      - name: until
        in: query
        type: string
        description: >-
          End date of when measurement were run (ex.
          "2016-10-20T10:30:00")

      - name: confirmed
        in: query
        type: string
        description: |
          Set "true" for confirmed network anomalies (we found a blockpage, a middlebox, etc.).
          Default: no filtering (show both true and false)

      - name: anomaly
        in: query
        type: string
        description: |
          Set "true" for measurements that require special attention (likely to be a case of blocking)
          Default: no filtering (show both true and false)

      - name: failure
        in: query
        type: string
        description: |
          Set "true" for failed measurements (the control request failed, there was a bug, etc.).
          Default: no filtering (show both true and false)

      - name: software_version
        in: query
        type: string
        description: Filter measurements by software version. Comma-separated.

      - name: test_version
        in: query
        type: string
        description: Filter measurements by test version. Comma-separated.

      - name: engine_version
        in: query
        type: string
        description: Filter measurements by engine version. Comma-separated.

      - name: order_by
        in: query
        type: string
        description: 'By which key the results should be ordered by (default: `null`)'
        enum:
          - test_start_time
          - measurement_start_time
          - input
          - probe_cc
          - probe_asn
          - test_name
      - name: order
        in: query
        type: string
        description: |-
          If the order should be ascending or descending (one of: `asc` or `desc`)
        enum:
          - asc
          - desc
          - ASC
          - DESC
      - name: offset
        in: query
        type: integer
        description: 'Offset into the result set (default: 0)'
      - name: limit
        in: query
        type: integer
        description: 'Number of records to return (default: 100)'
    responses:
      '200':
        description: Returns the list of measurement IDs for the specified criteria
        schema:
          $ref: "#/definitions/MeasurementList"
    """
    # x-code-samples:
    # - lang: 'curl'
    #    source: |
    #    curl "https://api.ooni.io/api/v1/measurements?probe_cc=IT&confirmed=true&since=2017-09-01"
    param = request.args.get
    report_id = param_report_id_or_none()
    probe_asn = param_asn("probe_asn")  # int / None
    probe_cc = param("probe_cc")
    test_name = param("test_name")
    since = param_date("since")
    until = param_date("until")
    order_by = param("order_by")
    order = param("order", "desc")
    offset = int(param("offset", 0))
    limit = int(param("limit", 100))
    failure = param_bool("failure")
    anomaly = param_bool("anomaly")
    confirmed = param_bool("confirmed")
    category_code = param("category_code")
    software_versions = param_commasplit("software_version")
    test_versions = param_commasplit("test_version")
    engine_versions = param_commasplit("engine_version")

    # Workaround for https://github.com/ooni/probe/issues/1034
    user_agent = request.headers.get("User-Agent", "")
    if user_agent.startswith("okhttp"):
        bug_probe1034_response = {
            "metadata": {
                "count": 1,
                "current_page": 1,
                "limit": 100,
                "next_url": None,
                "offset": 0,
                "pages": 1,
                "query_time": 0.001,
            },
            "results": [{"measurement_url": ""}],
        }
        # Cannot be cached due to user_agent
        return nocachejson(**bug_probe1034_response)

    # # Prepare query parameters

    input_ = request.args.get("input")
    domain = request.args.get("domain")

    # Set reasonable since/until ranges if not specified.
    try:
        if until is None:
            if report_id is None:
                t = datetime.utcnow() + timedelta(days=1)
                until = datetime(t.year, t.month, t.day)
    except ValueError:
        raise BadRequest("Invalid until")

    try:
        if since is None:
            if report_id is None and until is not None:
                since = until - timedelta(days=30)
    except ValueError:
        raise BadRequest("Invalid since")

    if order.lower() not in ("asc", "desc"):
        raise BadRequest("Invalid order")

    # # Perform query

    INULL = ""  # Special value for input = NULL to merge rows with FULL OUTER JOIN

    ## Create fastpath columns for query
    # TODO cast scores, coalesce input as ""
    fpwhere = []
    query_params: Dict[str, Any] = {}

    # Populate WHERE clauses and query_params dict

    if since is not None:
        query_params["since"] = since
        fpwhere.append(sql.text("measurement_start_time > :since"))

    if until is not None:
        query_params["until"] = until
        fpwhere.append(sql.text("measurement_start_time <= :until"))

    if report_id:
        query_params["report_id"] = report_id
        fpwhere.append(sql.text("report_id = :report_id"))

    if probe_cc:
        if probe_cc == "ZZ":
            log.info("Refusing list_measurements with probe_cc set to ZZ")
            abort(403)
        query_params["probe_cc"] = probe_cc
        fpwhere.append(sql.text("probe_cc = :probe_cc"))
    else:
        fpwhere.append(sql.text("probe_cc != 'ZZ'"))

    if probe_asn is not None:
        if probe_asn == 0:
            log.info("Refusing list_measurements with probe_asn set to 0")
            abort(403)
        query_params["probe_asn"] = probe_asn
        fpwhere.append(sql.text("probe_asn = :probe_asn"))
    else:
        # https://ooni.org/post/2020-ooni-probe-asn-incident-report/
        # https://github.com/ooni/explorer/issues/495
        fpwhere.append(sql.text("probe_asn != 0"))

    if test_name is not None:
        query_params["test_name"] = test_name
        fpwhere.append(sql.text("test_name = :test_name"))

    if software_versions is not None:
        query_params["software_versions"] = software_versions
        fpwhere.append(sql.text("software_version IN :software_versions"))

    if test_versions is not None:
        query_params["test_versions"] = test_versions
        fpwhere.append(sql.text("test_version IN :test_versions"))

    if engine_versions is not None:
        query_params["engine_versions"] = engine_versions
        fpwhere.append(sql.text("engine_version IN :engine_versions"))

    # Filter on anomaly, confirmed and failure:
    # The database stores anomaly and confirmed as boolean + NULL and stores
    # failures in different columns. This leads to many possible combinations
    # but only a subset is used.
    # On anomaly and confirmed: any value != TRUE is treated as FALSE
    # See test_list_measurements_filter_flags_fastpath

    if anomaly is True:
        fpwhere.append(sql.text("fastpath.anomaly = 't'"))

    elif anomaly is False:
        fpwhere.append(sql.text("fastpath.anomaly = 'f'"))

    if confirmed is True:
        fpwhere.append(sql.text("fastpath.confirmed = 't'"))

    elif confirmed is False:
        fpwhere.append(sql.text("fastpath.confirmed = 'f'"))

    if failure is True:
        fpwhere.append(sql.text("fastpath.msm_failure = 't'"))

    elif failure is False:
        fpwhere.append(sql.text("fastpath.msm_failure = 'f'"))

    fpq_table = sql.table("fastpath")

    if input_:
        # input_ overrides domain and category_code
        query_params["input"] = input_
        fpwhere.append(sql.text("input = :input"))

    elif domain or category_code:
        # both domain and category_code can be set at the same time
        if domain:
            query_params["domain"] = domain
            fpwhere.append(sql.text("domain = :domain"))

        if category_code:
            query_params["category_code"] = category_code
            fpq_table = fpq_table.join(
                sql.table("citizenlab"),
                sql.text("citizenlab.url = fastpath.input"),
            )
            fpwhere.append(sql.text("citizenlab.category_code = :category_code"))

    fp_query = select("*").where(and_(*fpwhere)).select_from(fpq_table)

    if order_by is None:
        order_by = "measurement_start_time"

    fp_query = fp_query.order_by(text("{} {}".format(order_by, order)))

    # Assemble the "external" query. Run a final order by followed by limit and
    # offset
    query = fp_query.offset(offset).limit(limit)
    query_params["param_1"] = limit
    query_params["param_2"] = offset

    # Run the query, generate the results list
    iter_start_time = time.time()

    try:
        rows = query_click(query, query_params)
        results = []
        for row in rows:
            msmt_uid = row["measurement_uid"]
            url = genurl("/api/v1/raw_measurement", measurement_uid=msmt_uid)
            results.append(
                {
                    "measurement_uid": msmt_uid,
                    "measurement_url": url,
                    "report_id": row["report_id"],
                    "probe_cc": row["probe_cc"],
                    "probe_asn": "AS{}".format(row["probe_asn"]),
                    "test_name": row["test_name"],
                    "measurement_start_time": row["measurement_start_time"],
                    "input": row["input"],
                    "anomaly": row["anomaly"] == "t",
                    "confirmed": row["confirmed"] == "t",
                    "failure": row["msm_failure"] == "t",
                    "scores": json.loads(row["scores"]),
                }
            )
    except OperationalError as exc:
        log.error(exc)
        if isinstance(exc.orig, QueryCanceledError):
            # FIXME: this is a postgresql exception!
            # Timeout due to a slow query. Generate metric and do not feed it
            # to Sentry.
            abort(504)

        raise exc

    # Replace the special value INULL for "input" with None
    for i, r in enumerate(results):
        if r["input"] == INULL:
            results[i]["input"] = None

    pages = -1
    count = -1
    current_page = math.ceil(offset / limit) + 1

    # We got less results than what we expected, we know the count and that
    # we are done
    if len(results) < limit:
        count = offset + len(results)
        pages = math.ceil(count / limit)
        next_url = None
    else:
        # XXX this is too intensive. find a workaround
        # count_start_time = time.time()
        # count = q.count()
        # pages = math.ceil(count / limit)
        # current_page = math.ceil(offset / limit) + 1
        # query_time += time.time() - count_start_time
        next_args = request.args.to_dict()
        next_args["offset"] = str(offset + limit)
        next_args["limit"] = str(limit)
        next_url = genurl("/api/v1/measurements", **next_args)

    query_time = time.time() - iter_start_time
    metadata = {
        "offset": offset,
        "limit": limit,
        "count": count,
        "pages": pages,
        "current_page": current_page,
        "next_url": next_url,
        "query_time": query_time,
    }
    return cachedjson("1m", metadata=metadata, results=results[:limit])


def set_dload(resp, fname: str):
    """Add header to make response downloadable"""
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}"


@api_msm_blueprint.route("/v1/torsf_stats")
@metrics.timer("get_torsf_stats")
def get_torsf_stats() -> Response:
    """Tor Pluggable Transports statistics
    Average / percentiles / total_count grouped by day
    Either group-by or filter by probe_cc
    Returns a format similar to get_aggregated
    ---
    parameters:
      - name: probe_cc
        in: query
        type: string
        description: The two letter country code
        minLength: 2
      - name: since
        in: query
        type: string
        description: >-
          The start date of when measurements were run (ex.
          "2016-10-20T10:30:00")
      - name: until
        in: query
        type: string
        description: >-
          The end date of when measurement were run (ex.
          "2016-10-20T10:30:00")
    responses:
      '200':
        description: Returns aggregated counters
    """
    param = request.args.get
    probe_cc = param("probe_cc")
    since = param("since")
    until = param("until")
    cacheable = False

    cols = [
        sql.text("toDate(measurement_start_time) AS measurement_start_day"),
        column("probe_cc"),
        sql.text("countIf(anomaly = 't') AS anomaly_count"),
        sql.text("countIf(confirmed = 't') AS confirmed_count"),
        sql.text("countIf(msm_failure = 't') AS failure_count"),
    ]
    table = sql.table("fastpath")
    where = [sql.text("test_name = 'torsf'")]
    query_params: Dict[str, Any] = {}

    if probe_cc:
        where.append(sql.text("probe_cc = :probe_cc"))
        query_params["probe_cc"] = probe_cc

    if since:
        where.append(sql.text("measurement_start_time > :since"))
        query_params["since"] = str(parse_date(since))

    if until:
        until_td = parse_date(until)
        where.append(sql.text("measurement_start_time <= :until"))
        query_params["until"] = str(until_td)
        cacheable = until_td < datetime.now() - timedelta(hours=72)

    # Assemble query
    where_expr = and_(*where)
    query = select(cols).where(where_expr).select_from(table)

    query = query.group_by(column("measurement_start_day"), column("probe_cc"))
    query = query.order_by(column("measurement_start_day"), column("probe_cc"))

    try:
        q = query_click(query, query_params)
        result = []
        for row in q:
            row = dict(row)
            row["anomaly_rate"] = row["anomaly_count"] / row["measurement_count"]
            result.append(row)
        response = jsonify({"v": 0, "result": result})
        if cacheable:
            response.cache_control.max_age = 3600 * 24
        return response

    except Exception as e:
        return jerror(str(e), v=0)


# # measurement feedback

from ooniapi.database import insert_click


"""
CREATE TABLE msmt_feedback
(
    `measurement_uid` String,
    `account_id` String,
    `status` String,
    `update_time` DateTime64(3) MATERIALIZED now64()
)
ENGINE = ReplacingMergeTree
ORDER BY (measurement_uid, account_id)
SETTINGS index_granularity = 4
"""

valid_feedback_status = [
    "blocked",
    "blocked.blockpage",
    "blocked.blockpage.http",
    "blocked.blockpage.dns",
    "blocked.blockpage.server_side",
    "blocked.blockpage.server_side.captcha",
    "blocked.dns",
    "blocked.dns.inconsistent",
    "blocked.dns.nxdomain",
    "blocked.tcp",
    "blocked.tls",
    "ok",
    "down",
    "down.unreachable",
    "down.misconfigured",
]


@api_msm_blueprint.route("/_/measurement_feedback/<measurement_uid>")
@metrics.timer("get_msmt_feedback")
def get_msmt_feedback(measurement_uid) -> Response:
    """Get measurement for the curred logged user for a given measurement
    ---
    produces:
      - application/json
    parameters:
      - name: measurement_uid
        in: path
        type: string
        description: Measurement ID
        minLength: 5
        required: true
    responses:
      200:
        description: status summary
    """
    account_id = get_account_id_or_none()
    query = """SELECT status, account_id = :aid AS is_mine, count() AS cnt
        FROM msmt_feedback FINAL
        WHERE measurement_uid = :muid
        GROUP BY status, is_mine
    """
    qp = dict(aid=account_id, muid=measurement_uid)
    rows = query_click(sql.text(query), qp)
    out: Dict[str, Any] = dict(summary={})
    for row in rows:
        status = row["status"]
        if row["is_mine"]:
            out["user_feedback"] = status
        out["summary"][status] = out["summary"].get(status, 0) + row["cnt"]

    return cachedjson("0s", **out)


@api_msm_blueprint.route("/_/measurement_feedback", methods=["POST"])
@metrics.timer("submit_msmt_feedback")
@role_required(["admin", "user"])
def submit_msmt_feedback() -> Response:
    """Submit measurement feedback. Only for registered users.
    ---
    produces:
      - application/json
    consumes:
      - application/json
    parameters:
      - in: body
        required: true
        schema:
          type: object
          properties:
            measurement_uid:
              type: string
              description: Measurement ID
            status:
              type: string
              description: Measurement status
              minLength: 2
    responses:
      200:
        description: Submission or update accepted
    """

    def jparam(name):
        return request.json.get(name, "").strip()

    account_id = get_account_id_or_none()
    status = jparam("status")
    if status not in valid_feedback_status:
        return jerror("Invalid status")
    measurement_uid = jparam("measurement_uid")

    query = "INSERT INTO msmt_feedback (measurement_uid, account_id, status) VALUES"
    query_params = [measurement_uid, account_id, status]
    insert_click(query, [query_params])
    return cachedjson("0s")
