FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS wheel

ARG MANGA_ISBN=false
ARG EPUB_CONVERTER=false
WORKDIR /build
RUN case "$MANGA_ISBN" in \
      false|'') ;; \
      true) printf '%s\n' 'MANGA_ISBN=true requires the original licensed addons/manga_isbn source, Chrome .deb and vendored python-anilist assets. They are absent from this checkout.' >&2; exit 2 ;; \
      *) printf '%s\n' 'MANGA_ISBN must be true or false.' >&2; exit 2 ;; \
    esac \
    && case "$EPUB_CONVERTER" in \
      false|'') ;; \
      true) printf '%s\n' 'EPUB_CONVERTER=true requires the original addons/epub_converter source and requirements.txt. They are absent from this checkout.' >&2; exit 2 ;; \
      *) printf '%s\n' 'EPUB_CONVERTER must be true or false.' >&2; exit 2 ;; \
    esac
COPY pyproject.toml uv.lock .python-version LICENSE.md blank_black.png blank_white.jpg ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir uv==0.11.32 \
    && uv export --locked --only-group dev --no-emit-project --format requirements.txt --output-file /tmp/build-requirements.txt \
    && uv pip install --system --require-hashes --requirement /tmp/build-requirements.txt \
    && uv build --python /usr/local/bin/python --wheel --no-build-isolation --out-dir /dist \
    && uv export --locked --extra all --no-default-groups --no-emit-project --no-editable --format requirements.txt --output-file /dist/requirements.txt

FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83

ARG PUID=1000
ARG PGID=1000
ENV PYTHONUNBUFFERED=1 \
    UMASK=022 \
    HOME=/home/appuser \
    XDG_STATE_HOME=/state
WORKDIR /app
RUN printf '%s\n' 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian bookworm non-free' > /etc/apt/sources.list.d/kce-non-free.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends unrar tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && case "$PUID:$PGID" in *[!0-9:]*|:*|*:) printf '%s\n' 'PUID and PGID must be positive numeric build arguments.' >&2; exit 2 ;; esac \
    && test "$PUID" -gt 0 && test "$PGID" -gt 0 \
    && groupadd --non-unique --gid "$PGID" appuser \
    && useradd --non-unique --create-home --uid "$PUID" --gid appuser appuser \
    && install --directory --owner=appuser --group=appuser /app /books /config /state
COPY --from=wheel /dist/*.whl /dist/requirements.txt /opt/kce-distribution/
RUN python -m pip install --no-cache-dir --require-hashes --requirement /opt/kce-distribution/requirements.txt \
    && set -- /opt/kce-distribution/*.whl \
    && test "$#" -eq 1 \
    && python -m pip install --no-cache-dir --no-deps "$1[all]" \
    && rm -rf /opt/kce-distribution
COPY --chown=appuser:appuser LICENSE.md komga_cover_extractor.py /app/
COPY --chown=appuser:appuser addons/qbit_torrent_unchecker/qbit_torrent_unchecker.py /app/addons/qbit_torrent_unchecker/
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER appuser
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["kce", "serve"]
