FROM mirror.gcr.io/library/node:24.13.0-alpine3.23 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY public/ ./public/
COPY src/ ./src/
RUN npm run build

FROM mirror.gcr.io/library/nginx:1.30.6-alpine3.24
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/
RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 8080
USER 101:101
