#!/usr/bin/env bash
# Hit /healthz on the local server and print the response.
curl -fsS http://localhost:8000/healthz && echo
