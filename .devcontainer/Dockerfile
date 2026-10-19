# The Python dev container image ships a Yarn apt source whose signing key has expired, so the first
# `apt-get update` (the Docker-in-Docker feature runs one) fails. Nothing here uses Yarn; drop the source.
FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn*.list
