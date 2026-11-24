import json
import logging

from app.core.logging_setup import REDACTED, RedactingFilter, get_logger, setup_logging


def test_redacts_known_secret_and_patterns(tmp_path):
    setup_logging(tmp_path, secrets=["SuperSecretValue42"], console=False)
    log = get_logger("test")
    log.info("using key SuperSecretValue42 now")
    log.info("header Authorization: Bearer abcdefghijklmnop1234")
    log.info("url ?api_key=zzzzzzzz&x=1", extra={"ctx": {"token": "SuperSecretValue42"}})
    for h in logging.getLogger("atlas").handlers:
        h.flush()
    lines = [json.loads(l) for l in (tmp_path / "atlas.jsonl").read_text().splitlines()]
    text = json.dumps(lines)
    assert "SuperSecretValue42" not in text
    assert "abcdefghijklmnop1234" not in text
    assert "zzzzzzzz" not in text
    assert text.count(REDACTED) >= 4
    assert all({"ts", "level", "logger", "msg"} <= set(l) for l in lines)


def test_filter_handles_format_args():
    f = RedactingFilter(["abcd1234"])
    rec = logging.LogRecord("atlas.x", logging.INFO, __file__, 1, "k=%s", ("abcd1234",), None)
    f.filter(rec)
    assert rec.getMessage() == f"k={REDACTED}"
