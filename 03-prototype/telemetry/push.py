"""
Minimal Grafana Cloud push clients: Prometheus remote_write and Loki.

Why hand-rolled protobuf: remote_write is a snappy-compressed protobuf POST, and the
official client pulls in a protobuf toolchain we do not want on the critical path four
days from a deadline. The WriteRequest schema is four nested messages; encoding it by
hand is ~60 lines and has no build step. See:
  WriteRequest { repeated TimeSeries timeseries = 1 }
  TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2 }
  Label        { string name = 1; string value = 2 }
  Sample       { double value = 1; int64 timestamp_ms = 2 }
"""
import os
import struct
import time

import cramjam
import requests

# ---------- protobuf wire format ----------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _string_field(field: int, s: str) -> bytes:
    return _bytes_field(field, s.encode("utf-8"))


def _double_field(field: int, v: float) -> bytes:
    return _tag(field, 1) + struct.pack("<d", v)


def _varint_field(field: int, v: int) -> bytes:
    return _tag(field, 0) + _varint(v)


def _encode_series(labels: dict, samples: list) -> bytes:
    # Mimir rejects unsorted label sets. Sort by name, always.
    body = b""
    for name in sorted(labels):
        lbl = _string_field(1, name) + _string_field(2, str(labels[name]))
        body += _bytes_field(1, lbl)
    for value, ts_ms in samples:
        smp = _double_field(1, float(value)) + _varint_field(2, int(ts_ms))
        body += _bytes_field(2, smp)
    return _bytes_field(1, body)


def encode_write_request(series: list) -> bytes:
    """series: [(labels_dict, [(value, ts_ms), ...]), ...]"""
    return b"".join(_encode_series(lbls, smps) for lbls, smps in series)


# ---------- clients ----------


class PromWriter:
    def __init__(self, url=None, user=None, token=None, session=None):
        self.url = url or os.environ["PROM_REMOTE_WRITE_URL"]
        self.user = user or os.environ["PROM_USER"]
        self.token = token or os.environ["GRAFANA_CLOUD_TOKEN"]
        self.s = session or requests.Session()

    def write(self, series: list, timeout=30):
        raw = encode_write_request(series)
        body = bytes(cramjam.snappy.compress_raw(raw))
        r = self.s.post(
            self.url,
            data=body,
            auth=(self.user, self.token),
            headers={
                "Content-Encoding": "snappy",
                "Content-Type": "application/x-protobuf",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
                "User-Agent": "second-unit-seeder/1.0",
            },
            timeout=timeout,
        )
        # 200 and 204 are both success; Mimir returns 4xx with a useful body.
        if r.status_code >= 400:
            raise RuntimeError(f"remote_write {r.status_code}: {r.text[:400]}")
        return r.status_code


class LokiWriter:
    def __init__(self, url=None, user=None, token=None, session=None):
        self.url = (url or os.environ["LOKI_PUSH_URL"]).rstrip("/")
        if not self.url.endswith("/loki/api/v1/push"):
            self.url += "/loki/api/v1/push"
        self.user = user or os.environ["LOKI_USER"]
        self.token = token or os.environ["GRAFANA_CLOUD_TOKEN"]
        self.s = session or requests.Session()

    def write(self, streams: list, timeout=30):
        """streams: [(labels_dict, [(ts_ns, line), ...]), ...]"""
        payload = {
            "streams": [
                {
                    "stream": {k: str(v) for k, v in lbls.items()},
                    # Loki wants each stream's entries in ascending time order.
                    "values": [[str(int(ts)), line] for ts, line in sorted(vals)],
                }
                for lbls, vals in streams
                if vals
            ]
        }
        if not payload["streams"]:
            return 204
        r = self.s.post(
            self.url,
            json=payload,
            auth=(self.user, self.token),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"loki push {r.status_code}: {r.text[:400]}")
        return r.status_code


def now_ms() -> int:
    return int(time.time() * 1000)
