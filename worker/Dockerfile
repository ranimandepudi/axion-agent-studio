# syntax=docker/dockerfile:1
# Resolve target-architecture wheels on the builder CPU. The runtime stage has
# no RUN instructions, so an x86 Cloud Build worker needs no ARM emulation.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS dependencies
ARG TARGETARCH
COPY worker/requirements.txt /requirements.txt
RUN case "$TARGETARCH" in arm64) wheel_arch=aarch64 ;; amd64) wheel_arch=x86_64 ;; *) exit 1 ;; esac \
    && pip install --no-cache-dir --only-binary=:all: \
       --platform "manylinux2014_${wheel_arch}" --platform "manylinux_2_28_${wheel_arch}" \
       --implementation cp --python-version 3.12 --abi cp312 \
       --target /deps -r /requirements.txt

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/deps
WORKDIR /app
COPY LICENSE LICENSE-SAMPLECODE LICENSE-SUMMARY NOTICE.md MODEL_SOURCES.md /app/
COPY --from=dependencies /deps /opt/deps
COPY worker /app/worker
USER 10001:10001
EXPOSE 8081
CMD ["python", "-m", "uvicorn", "worker.service:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
