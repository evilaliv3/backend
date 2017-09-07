import hashlib
import pytest
from measurements.pages import decompress_autoclaved

# The s3 URL is something like:
# https://s3.amazonaws.com/ooni-public/sanitised/2017-07-08/20170707T051609Z-IR-AS44244-web_connectivity-20170707T051545Z_AS44244_ujwFYcBJXcL2MjZnHXhBqVEOG2iHR0AuPUHB7aGGqUhdtvz70h-0.2.0-probe.json

# Note: The expected_shasum is that of the file inside of the new data
# pipeline, but it doesn't actually match the shasum ofthe files stored in s3
# which are generated by the old pipeline. This is due to minor processing
# differences in the two pipelines (non-deterministic JSON generation, some
# extra normalisation to clean the data format, etc.)
#
# Expected_shasum and report_size may be calculated like that:
# $ tar -I lz4 --to-stdout -x -f 2013-05-06/tcp_connect.0.tar.lz4 2013-05-06/20130506T113818Z-VN-AS45899-tcp_connect-no_report_id-0.1.0-probe.yaml | sha256sum -
# dc5e65ae1785afac7bb2d5c01b7d052954236c6e5a4e0bee38d0693cc52285bf  -
# $ tar -I lz4 --list -v -f 2013-05-06/tcp_connect.0.tar.lz4 2013-05-06/20130506T113818Z-VN-AS45899-tcp_connect-no_report_id-0.1.0-probe.yaml
# -rw-r--r-- ooni/torproject 865023 2017-02-22 15:16 2013-05-06/20130506T113818Z-VN-AS45899-tcp_connect-no_report_id-0.1.0-probe.yaml

AUTOCLAVED = {
    # A report spanning many frames, aligned with frame beginning and frame end.
    'many_frames': {
        # XXX this report is unexpectedly small. Pipeline puts small reports
        # into tar files, so this specific report is probably a bug, but it is
        # a useful test-case for a report taking whole file.
        'autoclaved_filename': '2016-10-02/20161001T062711Z-ID-AS23700-http_requests-20161001T062425Z_AS23700_GF6TnIwfUpH0xODHd3op9wQVNfa872gkZEhOxExNS9ASFgXMV2-0.2.0-probe.json.lz4',
        'textname': '2016-10-02/20161001T062711Z-ID-AS23700-http_requests-20161001T062425Z_AS23700_GF6TnIwfUpH0xODHd3op9wQVNfa872gkZEhOxExNS9ASFgXMV2-0.2.0-probe.json',
        'frame_off': 0,
        'total_frame_size': 496118 + 25272 - 0,
        'intra_off': 0,
        'report_size': 2835637,
        'expected_shasum': '419fc81dfefadd8ae7ccbb0bf272b1d7cf40e4b1e768539bb770b4da62601986',
    },

    # Single-measurement report fitting into a single frame with intra_offset == 0
    'single_frame_zero_offset': {
        'autoclaved_filename': '2017-07-08/http_header_field_manipulation.0.tar.lz4',
        'textname': '2017-07-08/20170707T035552Z-NL-AS8935-http_header_field_manipulation-20170707T060903Z_AS8935_cPNQyfUYfDVu5jC7mFV6rdSJ6djcLgrUvkEsVQdIYVupdTxJWU-0.2.0-probe.json',
        'frame_off': 23274,
        'total_frame_size': 23638,
        'intra_off': 0,
        'report_size': 2544,
        'expected_shasum': 'a26bea02f283fe99936f15a5be1bb9fd9e50a637170bbcf2a004eec0ae96f4a3'
    },

    # Single-measurement report fitting into a single frame with a non-zero intra_offset
    'single_frame_non_zero_offset': {
        'autoclaved_filename': '2016-07-07/http_invalid_request_line.0.tar.lz4',
        'textname': '2016-07-07/20160706T015518Z-CH-AS200938-http_invalid_request_line-20160706T015558Z_AS200938_iVsUREhX4hTEoHTOfyTFXyLvXKGGd80sFE3Xw3pJIUg2TDXr9I-0.2.0-probe.json',
        'frame_off': 0,
        'total_frame_size': 1897,
        'intra_off': 1536,
        'report_size': 3118,
        'expected_shasum': 'e13999959d636ad2b5fd8d50493a257a2c616b0adad086bf7211de5f09463f6d'
    },

    # Four-frame report starting and ending at the middle of the frame
    'multi_frame_mid_offset': {
        'autoclaved_filename': '2013-05-06/tcp_connect.0.tar.lz4',
        'textname': '2013-05-06/20130506T113818Z-VN-AS45899-tcp_connect-no_report_id-0.1.0-probe.yaml',
        'frame_off': 210860,
        'total_frame_size': 264195 + 17743 - 210860,
        'intra_off': 2381,
        'report_size': 865023,
        'expected_shasum': 'dc5e65ae1785afac7bb2d5c01b7d052954236c6e5a4e0bee38d0693cc52285bf',
    },

    # It's hard to find test-cases ending at the edge of non-last frame because
    # of tar padding and headers, but here is something like that.
    'next_frame_newline': {
        'autoclaved_filename': '2016-04-18/http_requests.06.tar.lz4',
        'textname': '2016-04-18/20160417T080443Z-GB-AS5607-http_requests-no_report_id-0.1.0-probe.yaml',
        'frame_off': 555968,
        'total_frame_size': 86597, # single frame, all alike reports fit into single frame :-/
        'intra_off': 211073,
        'report_size': 151041,
        'expected_shasum': '58dbc014da07fb3192e2231be37e1acc7344cf87ecc8afabf84bf5c387d1a825',
    },
}

@pytest.mark.parametrize("name,ac", AUTOCLAVED.items())
def test_decompress(client, name, ac):
    decompressor = decompress_autoclaved(
            ac['autoclaved_filename'],
            ac['frame_off'],
            ac['total_frame_size'],
            ac['intra_off'],
            ac['report_size'])
    download_size = 0
    h = hashlib.sha256()
    g = decompressor()
    all_data = 0
    for chunk in g:
        all_data += len(chunk)
        h.update(chunk)
    assert all_data == ac['report_size'], all_data
    assert h.hexdigest() == ac['expected_shasum'], h.hexdigest()
