# Supply an approved Red Hat UBI Python 3.12 image or mirror, pinned by digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0
COPY scripts/ipam.py /opt/vm/ipam.py
RUN python3 -m py_compile /opt/vm/ipam.py && \
    chgrp -R 0 /opt/vm && chmod -R g=u /opt/vm
USER 1001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/vm/ipam.py"]
