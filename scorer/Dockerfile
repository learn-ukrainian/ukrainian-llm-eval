FROM python:3.8-slim@sha256:1d52838af602b4b5a831beb13a0e4d073280665ea7be7f69ce2382f29c5a613f
COPY requirements.lock /opt/scorer/requirements.lock
RUN /usr/local/bin/python -m pip install --no-cache-dir pip==25.0.1
RUN /usr/local/bin/python -m pip install --no-cache-dir --only-binary=:all: --extra-index-url https://download.pytorch.org/whl/cpu -r /opt/scorer/requirements.lock
RUN /usr/local/bin/python -m spacy link en_core_web_sm en
RUN /usr/local/bin/python -c "import stanza; stanza.download('uk', processors='tokenize', model_dir='/opt/stanza', verbose=False)"
ENV STANZA_RESOURCES_DIR=/opt/stanza
COPY score.py resources.json /opt/scorer/
RUN /usr/local/bin/python -c "import sys; sys.path.insert(0, '/opt/scorer'); import score; score.verify_resources()"
ENTRYPOINT ["/usr/local/bin/python", "/opt/scorer/score.py"]
