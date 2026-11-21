You are worker {AGENT_ID} on the repo julian-kraus/Start-Lio. Read AGENTS.md first and follow it exactly.

Do one round:
1. If an issue is already claimed by {AGENT_ID} (claim comment or claim commit on its branch i<n>) and not
   merged, continue it in its worktree .worktrees/i<n>. Also fix any of your PRs labelled review:changes first.
   Whatever you work on, first comment `working: {AGENT_ID}` on that issue (this is your heartbeat).
2. Otherwise pick and claim one issue as described in AGENTS.md (highest priority status:ready, blockers closed,
   no file overlap with claimed issues, git claim lock on branch i<n>, then `python ops/gh_status.py claim`).
3. Implement it in a fresh worktree under .worktrees/, test it, push, open the PR with `Closes #<n>`, the acceptance checklist with
   evidence, and a line `Agent: {AGENT_ID}`. Then `python ops/gh_status.py review <n>`.
4. Comment progress on the issue at least every 30 minutes of work.

End your round with one line of plain text: what you did and the issue number. If there was nothing you could
pick or continue, end with exactly {NO_WORK}.
