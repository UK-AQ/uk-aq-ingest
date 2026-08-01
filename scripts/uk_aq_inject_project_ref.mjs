import fs from 'node:fs/promises';
import path from 'node:path';

const projectRef = process.env.SUPABASE_PROJECT_REF || '';
const publishableKey = process.env.SB_PUBLISHABLE_DEFAULT_KEY || '';

const replacements = [
  { name: 'SUPABASE_PROJECT_REF', value: projectRef },
  { name: 'SB_PUBLISHABLE_DEFAULT_KEY', value: publishableKey },
];

const allowedExtensions = new Set(['.html', '.js', '.mjs', '.ts', '.tsx', '.json']);
const skipDirNames = new Set(['.git', 'node_modules', 'archive']);
const root = process.cwd();

function tokenVariants(name) {
  return [`__${name}__`, `{{${name}}}`];
}

function parseOverridePaths() {
  const raw = process.env.UK_AQ_INJECT_PATHS;
  if (!raw) {
    return [];
  }
  return raw
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value) => (path.isAbsolute(value) ? value : path.join(root, value)));
}

function shouldSkipDir(dirPath) {
  const parts = dirPath.split(path.sep);
  return parts.some((part) => skipDirNames.has(part));
}

async function walk(dir, files) {
  if (shouldSkipDir(dir)) {
    return;
  }
  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      await walk(fullPath, files);
      continue;
    }
    if (!allowedExtensions.has(path.extname(entry.name))) {
      continue;
    }
    files.push(fullPath);
  }
}

function resolveMissingTokens(missingTokens) {
  if (!missingTokens.size) {
    return;
  }
  const missingList = Array.from(missingTokens).join(', ');
  throw new Error(`Missing env for token(s): ${missingList}`);
}

async function injectIntoFile(filePath, missingTokens) {
  const original = await fs.readFile(filePath, 'utf8');
  let updated = original;

  for (const replacement of replacements) {
    const variants = tokenVariants(replacement.name);
    for (const token of variants) {
      if (!updated.includes(token)) {
        continue;
      }
      if (!replacement.value) {
        missingTokens.add(replacement.name);
        continue;
      }
      updated = updated.split(token).join(replacement.value);
    }
  }

  if (updated !== original) {
    await fs.writeFile(filePath, updated, 'utf8');
    return true;
  }
  return false;
}

async function main() {
  const overridePaths = parseOverridePaths();
  const targetFiles = [];
  if (overridePaths.length) {
    for (const targetPath of overridePaths) {
      targetFiles.push(targetPath);
    }
  } else {
    await walk(root, targetFiles);
  }

  if (!targetFiles.length) {
    console.log('No files found for injection.');
    return;
  }

  const updatedFiles = [];
  const missingTokens = new Set();

  for (const filePath of targetFiles) {
    try {
      const changed = await injectIntoFile(filePath, missingTokens);
      if (changed) {
        updatedFiles.push(path.relative(root, filePath));
      }
    } catch (error) {
      if (overridePaths.length) {
        throw error;
      }
    }
  }

  resolveMissingTokens(missingTokens);

  if (updatedFiles.length) {
    console.log(`Injected Supabase values into ${updatedFiles.length} file(s):`);
    for (const filePath of updatedFiles) {
      console.log(`- ${filePath}`);
    }
    return;
  }

  console.log('No injection placeholders found; skipping.');
}

main().catch((error) => {
  console.error('Project ref injection failed:', error.message || error);
  process.exit(1);
});
