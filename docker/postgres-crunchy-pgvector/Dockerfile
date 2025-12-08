FROM postgres:15

# Install build dependencies and compile/install pgvector
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git postgresql-server-dev-15 ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector && make && make install \
    && rm -rf /tmp/pgvector

# Ensure ownership/permissions are correct
RUN chown -R postgres:postgres /var/lib/postgresql || true

USER postgres
