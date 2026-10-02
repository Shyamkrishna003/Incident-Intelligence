# syntax=docker/dockerfile:1
# Firebase Auth emulator for local development and tests. It issues UNSIGNED tokens, so it
# must never be used in production (the API refuses to start that way).
FROM node:22-alpine

RUN --mount=type=cache,target=/root/.npm \
    npm install --global --no-fund --no-audit firebase-tools@15.32.1

WORKDIR /srv
COPY firebase.json ./
EXPOSE 9099

# A project id starting with "demo-" keeps the emulator fully offline: it never contacts
# real Firebase services.
CMD ["firebase", "emulators:start", "--only", "auth", "--project", "demo-incident-intel"]
