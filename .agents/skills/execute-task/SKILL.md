---
name: execute-task
description: >-
  Executes the current technical specification and task plan documented in
  .agents/current_task.md. Use this skill whenever implementing a feature,
  refactoring, or fixing a bug defined in the architecture and analysis chat.
---

# Execute Current Task Specification

This skill executes the crystallized technical specification (ТЗ) prepared in the Architecture and Discussion chat.

## Workflow

1. **Read the Specification:**
   - Always start by viewing `.agents/current_task.md` using `view_file`.
   - Carefully verify the problem statement, root cause, and required atomic steps.

2. **Strict Scope Discipline (Non-Negotiable):**
   - Implement **ONLY** what is explicitly described in `.agents/current_task.md`.
   - Do NOT rewrite or refactor unrelated working sections of the codebase.
   - Do NOT introduce unrequested modules, scrapers, or analytics.
   - Maintain atomic edits without leaving placeholders like `// rest of code remains the same`.

3. **Framework Safety Rules:**
   - **Playwright:** No dangerous immediate accesses to dynamic elements without checking existence or safely wrapping in `try...except` with proper timeouts and clean resource disposal (`finally: await browser.close()`).
   - **React (if applicable):** Strictly observe Rules of Hooks (all hooks at the very top of components).

4. **Verification & Testing:**
   - Ensure syntax and lint checks pass.
   - Run tests or relevant verification scripts as specified in the task.

5. **Reporting Format:**
   - List modified files (`File Path`) and bullet-point summary of actions.
   - Provide a single git command:
     `git add . && git commit -m "..." && git push origin main`
