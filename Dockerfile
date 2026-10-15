FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY curtail ./curtail
RUN pip install --no-cache-dir .
VOLUME ["/data"]
# Mount your config at /config/config.yaml and set data_dir: /data in it.
# Use host networking so the agent can reach miners on your LAN:
#   docker run -d --network host -v ./config.yaml:/config/config.yaml -v curtail-data:/data loadsettle-curtail
ENTRYPOINT ["curtail"]
CMD ["run", "-c", "/config/config.yaml"]
