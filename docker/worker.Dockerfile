# Linux x86_64 only. PCBWorld is built against KiCad 9.0.8 + kicad-python 0.6.0.
FROM --platform=linux/amd64 ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends software-properties-common ca-certificates git curl \
    python3 python3-pip python3-venv default-jre-headless \
 && add-apt-repository -y ppa:kicad/kicad-9.0-releases && apt-get update \
 && apt-get install -y --no-install-recommends kicad && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt huggingface_hub torch --extra-index-url https://download.pytorch.org/whl/cpu
# PCBWorld engine: follow the repo's install steps (pins KiCad 9.0.8). Non-commercial/research use only.
RUN git clone https://github.com/LGAI-Research/PCBWorld /opt/PCBWorld \
 && pip3 install --break-system-packages -e /opt/PCBWorld || echo "!! finish PCBWorld install per its README"
RUN hf download LG-AI-Research/PCBWorld --local-dir /app/var/checkpoints/pcbworld || echo "!! download checkpoints at runtime"
RUN pip3 install --break-system-packages --no-cache-dir gymnasium pyyaml
# Freerouting fallback: drop freerouting.jar at /opt/freerouting.jar
COPY coupled_backend ./coupled_backend
ENV DATA_DIR=/data CHECKPOINT_DIR=/app/var/checkpoints/pcbworld PCBWORLD_REPO=/opt/PCBWorld PYTHONPATH=/opt/PCBWorld ROUTER_ENGINE=auto
CMD ["uvicorn", "coupled_backend.worker_app:app", "--host", "0.0.0.0", "--port", "8001"]
