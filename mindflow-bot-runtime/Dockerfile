FROM crpi-8tbzhfoaax5hq44s.cn-shanghai.personal.cr.aliyuncs.com/mind_flow/python
ARG BUILD_REVISION=development
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/runtime:/srv/project \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/home/mindflow \
    CLAUDE_WORKDIR=/srv/claude-workspace \
    BUILD_REVISION=${BUILD_REVISION}

WORKDIR /srv/runtime
COPY mindflow-bot-runtime/requirements.txt ./requirements.txt
RUN for source_file in \
      /etc/apt/sources.list.d/debian.sources \
      /etc/apt/sources.list; do \
      if [ -f "$source_file" ] && grep -q 'deb.debian.org' "$source_file"; then \
        sed -i \
          -e 's|https://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
          -e 's|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
          -e 's|https://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
          -e 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
          "$source_file"; \
      fi; \
    done \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      fontconfig \
      fonts-noto-cjk \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
RUN pip install \
    --no-cache-dir \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    -r requirements.txt
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
 && mkdir -p /tmp/matplotlib \
 && chown -R mindflow:mindflow /srv/runtime /srv/project /srv/claude-workspace /home/mindflow /tmp/matplotlib
USER mindflow

CMD ["python", "-m", "app.main"]
