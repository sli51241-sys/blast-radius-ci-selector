# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# A script works with both the classic Docker builder and BuildKit.
COPY tools/fetch_source.py /app/fetch_source.py
RUN python /app/fetch_source.py
# Fresh import probes can read validated bytecode without sharing module state.
RUN python -m compileall -q --invalidation-mode checked-hash /opt/sqlglot/sqlglot
COPY selector.py scheduler.py patch_analysis.py class_dependencies.py import_probe.py lambda_analysis.py class_projection.py factory_analysis.py ast_compare.py runtime_body.py /app/
COPY maps/ /app/maps/
CMD ["python", "/app/selector.py"]
