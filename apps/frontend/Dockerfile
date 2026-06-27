# Next.js (App Router) — multi-stage build to a small standalone runtime image.
# NEXT_PUBLIC_* is inlined at BUILD time for client components, so the API base URL
# is a build arg (defaults to the host-mapped API port for browser access).
FROM node:22-alpine AS base
RUN corepack enable
WORKDIR /app

# --- deps ---
FROM base AS deps
# pnpm-workspace.yaml carries the sharp build approval; pnpm 11 errors without it.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# --- build ---
FROM base AS build
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

# --- runtime ---
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
