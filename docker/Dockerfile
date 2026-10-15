# Container image of Spring PetClinic for the measurement workflow.
#
# Build context: app/target (see Dockerfile.dockerignore). The workflow passes
# the base image by digest from tools/versions.json (GHCR mirror) and the
# correlation ID of the run. There is no RUN instruction, so the build runs no
# code. No syntax directive, so BuildKit uses its built-in frontend and does not
# pull a frontend image.
#
# The default of BASE_IMAGE is the same mirror digest, so FROM has a valid
# reference without build arguments (BuildKit check InvalidDefaultArgInFrom).
# tools/check_workflows.py checks that it equals tools/versions.json.
ARG BASE_IMAGE=ghcr.io/vermout94/mcs-pipeline/base/eclipse-temurin@sha256:d9a39a23634650173f1e2bbc176227af9728587ecf0f4b62d53e9355cd7a19ab
FROM ${BASE_IMAGE}

ARG CORRELATION_ID
LABEL org.opencontainers.image.source="https://github.com/vermout94/mcs-pipeline" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.github.vermout94.mcs-pipeline.correlation-id="${CORRELATION_ID}"

COPY spring-petclinic-4.0.0-SNAPSHOT.jar /app/app.jar

# User ubuntu (uid and gid 1000) of the base image.
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
