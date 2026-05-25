# mediacast — phone-facing HTTP receiver for "open this URL on the
# projector". Validates a bearer token, sanity-checks the URL, then
# forwards the request to the host-side helper (mediacast-host.py)
# running as a systemd --user unit on the box itself. The host helper
# owns the X11/Firefox work, which is awkward to do from inside a
# container (xauth, fonts, GL, audio devices).
#
# This container's whole job is: be the trust boundary, expose the
# LAN-facing endpoint, and keep the docker-compose pattern uniform
# with everything else on this box.

FROM python:3.12-alpine

RUN adduser -D -u 1000 mediacast

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

USER mediacast

EXPOSE 8765

# Single worker — this is a one-user "send me a URL" endpoint, not a
# fan-out service. Bind 0.0.0.0 because the container's port is
# published to the host LAN via docker-compose.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765", "--log-level", "info"]
