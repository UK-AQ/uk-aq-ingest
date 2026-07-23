// scripts/keepalive.mjs
import 'dotenv/config';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY =
  process.env.SB_PUBLISHABLE_DEFAULT_KEY ||
  process.env.SB_SECRET_KEY;
const KEEPALIVE_TABLE = process.env.KEEPALIVE_TABLE || 'keep_alive';
const KEEPALIVE_SELECT = process.env.KEEPALIVE_SELECT || '*';

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error(
    'Missing SUPABASE_URL or a key (SB_PUBLISHABLE_DEFAULT_KEY / SB_SECRET_KEY).'
  );
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

async function main() {
  // Using head: true + limit(1) avoids transferring rows.
  const { error } = await supabase
    .from(KEEPALIVE_TABLE)
    .select(KEEPALIVE_SELECT, { head: true })
    .limit(1);

  if (error) {
    console.error('Keepalive query failed:', error);
    process.exit(1);
  }
  console.log(`Keepalive OK table=${KEEPALIVE_TABLE}`);
}

main().catch((error) => {
  console.error('Keepalive crashed:', error);
  process.exit(1);
});
