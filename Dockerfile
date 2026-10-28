# Review exact context identities and build authorization before invoking a builder.
ARG BASE_IMAGE
ARG TARGET_PLATFORM
FROM --platform=${TARGET_PLATFORM} ${BASE_IMAGE}
ENV PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/host
WORKDIR /opt/host
COPY wheels/ /opt/wheels/
COPY locks/runtime.lock /opt/locks/runtime.lock
RUN python -m pip install --no-index --no-cache-dir --no-compile --require-hashes \
    --find-links=/opt/wheels -r /opt/locks/runtime.lock \
    && python -m pip check \
    && rm -rf /opt/wheels
COPY host/ /opt/host/
USER 65532:65532
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
