# CS Platform container image.
#
# The platform server is Python-stdlib-only at runtime (no framework), but the live
# adapters use boto3 (Redshift Data API churn) and the tests use pytest. We install
# only the runtime deps needed for live operation.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CS_PORT=8787

WORKDIR /app

# boto3 is the only runtime third-party dependency (Redshift churn adapter). Everything
# else the server uses is Python stdlib. Pin via requirements for reproducibility.
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Application code.
COPY platform/ ./platform/
COPY plugins/ ./plugins/

# Run as an unprivileged user (matches the ja-observe hardening posture).
RUN useradd --uid 1000 --create-home appuser
USER 1000:1000

EXPOSE 8787

# The server reads PORT-equivalent from argv; default 8787. Auth is enforced when
# AUTH_COGNITO_* (or AUTH_DEV_LOGIN) is configured via the task definition.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
  CMD python3 -c "import urllib.request,sys; \
    req=urllib.request.Request('http://localhost:8787/login'); \
    sys.exit(0) if urllib.request.urlopen(req, timeout=3).status in (200,302) else sys.exit(1)" || exit 1

CMD ["python3", "platform/server.py", "8787"]
