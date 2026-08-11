FROM python:3.11.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/runtime:/srv/project \
    HOME=/home/mindflow \
    CLAUDE_WORKDIR=/srv/claude-workspace

WORKDIR /srv/runtime
COPY mindflow-bot-runtime/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import claude_agent_sdk; print('claude-agent-sdk import ok')"

COPY mindflow-bot-runtime/ /srv/runtime/
COPY algorithm/ /srv/project/algorithm/
COPY calibration/ /srv/project/calibration/
COPY core_engine/ /srv/project/core_engine/
COPY entity/ /srv/project/entity/
COPY event/ /srv/project/event/
COPY settings/ /srv/project/settings/
COPY strategy/ /srv/project/strategy/
COPY utils/ /srv/project/utils/
COPY entry/ /srv/project/entry/
COPY services/ /srv/project/services/
COPY claude-runtime/ /srv/claude-workspace/

RUN useradd --create-home --uid 10001 mindflow \
 && chown -R mindflow:mindflow /srv/runtime /srv/project /srv/claude-workspace /home/mindflow
USER mindflow

CMD ["python", "-m", "app.main"]
