FROM denoland/deno:2.1.4

WORKDIR /app

COPY workers/uk_aq_observs_pubsub_cloud_run/run_job.ts /app/workers/uk_aq_observs_pubsub_cloud_run/run_job.ts
COPY workers/uk_aq_observs_pubsub_cloud_run/run_service.ts /app/workers/uk_aq_observs_pubsub_cloud_run/run_service.ts
COPY supabase/functions/_shared/fetch_egress_patch.ts /app/supabase/functions/_shared/fetch_egress_patch.ts
COPY supabase/functions/_shared/egress_metrics.ts /app/supabase/functions/_shared/egress_metrics.ts
COPY supabase/functions/_shared/observs_client.ts /app/supabase/functions/_shared/observs_client.ts

ENV PORT=8000
ENV OBSERVS_PUBSUB_LOCAL_PORT=8000

CMD ["deno", "run", "--allow-env", "--allow-net", "--allow-read", "--allow-write", "--allow-run", "/app/workers/uk_aq_observs_pubsub_cloud_run/run_service.ts"]
