export function ciPortCacheKey(environment) {
  if (!environment.CI) return '';

  const run = environment.GITHUB_RUN_ID
    || environment.GITHUB_RUN_NUMBER
    || environment.GITHUB_SHA
    || 'ci';
  const attempt = environment.GITHUB_RUN_ATTEMPT || '1';
  const job = environment.GITHUB_JOB || 'job';
  return `${run}-${attempt}-${job}`.replace(/[^a-zA-Z0-9_-]/g, '_');
}
