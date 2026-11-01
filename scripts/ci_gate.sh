#!/usr/bin/env bash
set -euo pipefail

[ "$PLAN_RESULT" = success ] || { echo 'CI plan failed'; exit 1; }
expected=success
case "$REUSE" in
  true) expected=skipped ;;
  false) ;;
  *) echo 'CI reuse decision missing or invalid'; exit 1 ;;
esac
read -r -a job_results <<< "$RESULTS"
[ "${#job_results[@]}" -eq 5 ] || { echo 'Required job results missing'; exit 1; }
for result in "${job_results[@]}"; do
  [ "$result" = "$expected" ] || { echo "A required job was $result, expected $expected"; exit 1; }
done
