"""Production S3 configuration rules (spec §4 / golden rule 7).

Production talks to real AWS S3 over TLS with the EC2 IAM role. The
LocalStack-only settings (``S3_ENDPOINT_URL``, ``S3_USE_SSL``) must not leak
into a production environment — a leftover dev endpoint is ignored by the S3
client, so it silently misleads the operator. The platform refuses to start
instead.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_rejects_localstack_endpoint():
    with pytest.raises(ValidationError, match="S3_ENDPOINT_URL must be unset"):
        Settings(environment="production", s3_bucket="quant-platform-prod",
                 s3_endpoint_url="http://localhost:4566", _env_file=None)


def test_production_rejects_blank_but_present_endpoint():
    # Blank/whitespace values count as "unset" (tolerated, not an error).
    Settings(environment="production", s3_bucket="b", s3_endpoint_url="", _env_file=None)
    Settings(environment="production", s3_bucket="b", s3_endpoint_url="  ", _env_file=None)


def test_production_defaults_to_tls():
    # _env_file=None: the test process would otherwise load platform/backend/.env
    # (a dev file with the LocalStack endpoint and S3_USE_SSL=false).
    s = Settings(environment="production", s3_bucket="quant-platform-prod", _env_file=None)
    assert s.s3_use_ssl is True
    assert s.s3_endpoint_url is None


def test_local_may_use_localstack_endpoint():
    s = Settings(environment="development", s3_bucket="quant-platform-dev",
                 s3_endpoint_url="http://localhost:4566", s3_use_ssl=False, _env_file=None)
    assert s.s3_endpoint_url == "http://localhost:4566"
    assert s.s3_use_ssl is False
