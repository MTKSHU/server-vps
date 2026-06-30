FROM node:22-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json tsconfig.json vite.config.ts ./
COPY index.html ./index.html
COPY src ./src
RUN npm ci --include=optional && npm run build

FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
