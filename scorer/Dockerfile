FROM cgr.dev/chainguard/wolfi-base@sha256:be019c1c74fe69204a42c4e10bc4d886a49bf5403ffa74374ef7baa408fc9a26
COPY requirements.lock /opt/scorer/requirements.lock
RUN apk add --no-cache python-3.11=3.11.18-r2 libstdc++=16.2.0-r5 \
    && apk add --no-cache --virtual .scorer-build py3.11-pip=26.2.2-r3 \
    && /usr/bin/python3.11 -m venv --without-pip /usr/local \
    && /usr/bin/python3.11 -m pip --python /usr/local install --no-cache-dir --only-binary=:all: -r /opt/scorer/requirements.lock \
    && apk del .scorer-build
ENV PATH=/usr/local/bin:$PATH
RUN /usr/local/bin/python -m spacy link en_core_web_sm en
ENV STANZA_RESOURCES_DIR=/opt/stanza
COPY score.py resources.json /opt/scorer/
RUN cd /opt/scorer && /usr/local/bin/python - <<'PY'
import stanza
from score import STANZA_PROCESSORS, verify_resources

stanza.download("uk", package=None, processors=STANZA_PROCESSORS, resources_version="1.4.1", verbose=False)
verify_resources()
PY
RUN chmod -R a+rX /opt/stanza
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "/opt/scorer/score.py"]
