"""
Incident reporting

https://docs.google.com/document/d/1TdMBWD45j3bx7GRMIriMvey72viQeKrx7Ad6DyboLwg/

Create / update / delete / list incidents
Search/list incidents with:
    Filtering by domain/cc/asn/creator id/ and so on
    Sort by creation/edit date, event date, and so on
    Each incident has one link to the mat (or more?)

Users can only update/delete incidents created by themselves.
Admins can update/delete everything.

"""

from datetime import datetime
import logging

from flask import Blueprint, current_app, request, Response

from ooniapi.auth import (
    role_required,
    get_client_role,
    get_account_id_or_none,
    get_account_id_or_raise,
)
from ooniapi.config import metrics
from ooniapi.database import query_click, optimize_table, insert_click, raw_query
from ooniapi.errors import BaseOONIException, jerror
from ooniapi.errors import OwnershipPermissionError, InvalidRequest
from ooniapi.urlparams import param_bool
from ooniapi.utils import nocachejson, generate_random_intuid

log: logging.Logger

# Support multi-node Clickhouse with ReplacingMergeTree
# and update/delete without having to read the whole table before.
# Updates/deletes are performed by ReplacingMergeTree based on update_time
# against (title, event_type). We are not checking for the correct values
# in the old dict during update/delete.
# "deleted" will be used automatically by Clickhouse version 23.3
#
# The table creation for CI purposes is in tests/integ/clickhouse_1_schema.sql

inc_blueprint = Blueprint("incidents_api", "incidents")


@metrics.timer("search_list_incidents")
@inc_blueprint.route("/api/v1/incidents/search", methods=["GET"])
def search_list_incidents() -> Response:
    """Search and list incidents
    ---
    parameters:
      - name: only_mine
        in: query
        type: boolean
        description: Show only owned items
    responses:
      200:
        schema:
          type: object
    """
    # TODO: filter by CC, domain etc
    # TODO: configurable ORDER BY?
    global log
    log = current_app.logger
    log.debug("listing incidents")
    try:
        where = "WHERE deleted != 1"
        query_params = {}

        account_id = get_account_id_or_none()
        if param_bool("only_mine"):
            if account_id is None:
                return nocachejson(incidents=[])
            where += "\nAND creator_account_id = %(account_id)s"

        if account_id is None:
            # non-published incidents are not exposed to anon users
            where += "\nAND published = 1"
            query_params["account_id"] = "never-match"
        else:
            query_params["account_id"] = account_id

        query = f"""SELECT id, update_time, start_time, end_time, reported_by,
        title, event_type, published, CCs, ASNs, domains, tags, test_names,
        links, short_description, creator_account_id = %(account_id)s AS mine
        FROM incidents FINAL
        {where}
        ORDER BY title
        """
        q = query_click(query, query_params)
        rows = list(q)
        for r in rows:
            r["published"] = bool(r["published"])
        return nocachejson(incidents=rows, v=1)
    except BaseOONIException as e:
        return jerror(e)


@metrics.timer("show_incident")
@inc_blueprint.route("/api/v1/incidents/show/<string:incident_id>", methods=["GET"])
def show_incident(incident_id: str) -> Response:
    """Returns an incident
    ---
    parameters:
      - name: id
        in: query
        type: string
    responses:
      200:
        type: object
    """
    global log
    log = current_app.logger
    log.debug("showing incident")
    try:
        where = "WHERE id = %(id)s AND deleted != 1"
        account_id = get_account_id_or_none()
        if account_id is None:
            # non-published incidents are not exposed to anon users
            where += "\nAND published = 1"
            query_params = {"id": incident_id, "account_id": "never-match"}
        else:
            query_params = {"id": incident_id, "account_id": account_id}

        query = f"""SELECT id, update_time, start_time, end_time, reported_by,
        title, text, event_type, published, CCs, ASNs, domains, tags, test_names,
        links, short_description, creator_account_id = %(account_id)s AS mine
        FROM incidents FINAL
        {where}
        LIMIT 1
        """
        q = query_click(query, query_params)
        if len(q) < 1:
            return jerror("Not found")
        inc = q[0]
        inc["published"] = bool(inc["published"])
        # TODO: cache if possible
        return nocachejson(incident=inc, v=1)
    except BaseOONIException as e:
        return jerror(e)


def prepare_incident_dict(d: dict):
    d["creator_account_id"] = get_account_id_or_raise()
    exp = [
        "ASNs",
        "CCs",
        "creator_account_id",
        "domains",
        "end_time",
        "event_type",
        "id",
        "links",
        "published",
        "reported_by",
        "short_description",
        "start_time",
        "tags",
        "test_names",
        "text",
        "title"
    ]
    if sorted(d) != exp:
        log.debug(f"Invalid incident update request. Keys: {sorted(d)}")
        raise InvalidRequest()

    d["start_time"] = datetime.strptime(d["start_time"], "%Y-%m-%dT%H:%M:%SZ")
    if d["end_time"] is not None:
        d["end_time"] = datetime.strptime(d["end_time"], "%Y-%m-%dT%H:%M:%SZ")
        delta = d["end_time"] - d["start_time"]
        if delta.total_seconds() < 0:
            raise InvalidRequest()

    if not d["title"] or not d["text"]:
        log.debug("Invalid incident update request: empty title or desc")
        raise InvalidRequest()

    try:
        for asn in d["ASNs"]:
            int(asn)
    except Exception:
        raise InvalidRequest()


def user_cannot_update(incident_id: str) -> bool:
    # Check if there is already an incident and belogs to a different user
    query = """SELECT count() AS cnt
    FROM incidents FINAL
    WHERE deleted != 1
    AND id = %(incident_id)s
    AND creator_account_id != %(account_id)s
    """
    account_id = get_account_id_or_raise()
    query_params = dict(incident_id=incident_id, account_id=account_id)
    q = query_click(query, query_params)
    log.debug("An incident beloging to a different user has been found")
    return q[0]["cnt"] > 0


@metrics.timer("post_update_incident")
@inc_blueprint.route("/api/v1/incidents/<string:action>", methods=["POST"])
@role_required(["admin", "user"])
def post_update_incident(action: str) -> Response:
    """Create/update/delete an incident.
    The `action` value can be "create", "update", "delete".
    The `id` value is returned by the API when an incident is created.
    It should be set by the caller for incident update or deletion.
    ---
    parameters:
      - in: body
        name: Incident data
        schema:
          type: object
          properties:
            id:
              type: string
            title:
              type: string
            short_description:
              type: string
            start_time:
              type: string
            reported_by:
              type: string
            text:
              type: string
            published:
              type: boolean
            event_type:
              type: string
            ASNs:
              type: array
              items:
                type: integer
            CCs:
              type: array
              items:
                type: string
            tags:
              type: array
              items:
                type: string
            test_names:
              type: array
              items:
                type: string
            domains:
              type: array
              items:
                type: string
            links:
              type: array
              items:
                type: string
    responses:
      200:
        type: object
    """
    """
            end_time:
              type: string
              nullable: true
    """
    # See comments on top of file
    global log
    log = current_app.logger
    if action not in ("create", "update", "delete"):
        return jerror("Invalid request")  # TODO

    try:
        req = request.json
        if req is None:
            raise InvalidRequest()

        req["published"] = int(req.get("published", 0))

        if action in ("update", "delete"):
            incident_id = req.get("id")
            if incident_id is None:
                raise InvalidRequest()
            incident_id = str(incident_id)

        if action == "create":
            incident_id = str(generate_random_intuid(current_app))
            req["id"] = incident_id
            if get_client_role() != "admin" and req["published"] == 1:
                raise InvalidRequest

            log.info(f"Creating incident {incident_id}")

        elif action == "update":
            if get_client_role() != "admin":
                if user_cannot_update(incident_id):
                    raise OwnershipPermissionError
                if req["published"] == 1:
                    raise InvalidRequest

            log.info(f"Updating incident {incident_id}")

        elif action == "delete":
            if get_client_role() != "admin":
                if user_cannot_update(incident_id):
                    raise OwnershipPermissionError
            # TODO: switch to faster deletion with new db version
            q = "ALTER TABLE incidents DELETE WHERE id = %(iid)s"
            raw_query(q, {"iid": incident_id})
            optimize_table("incidents")
            return nocachejson()

        ins_sql = """INSERT INTO incidents
        (id, start_time, end_time, creator_account_id, reported_by, title,
        text, event_type, published, CCs, ASNs, domains, tags, links,
        test_names, short_description)
        VALUES
        """
        prepare_incident_dict(req)
        r = insert_click(ins_sql, [req])
        log.debug(f"Result: {r}")
        optimize_table("incidents")
        return nocachejson(r=r, id=incident_id)

    except Exception as e:
        log.info(e, exc_info=True)
        return jerror(e)
