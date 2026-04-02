---
name: reorganize
description: "Reorganize code file structure: rearrange modules, extract reuse, remove redundancy, add section comments. Preserves all functionality and logic."
argument-hint: "<file/dir or 'diff'>"
---

# Code File Reorganization

Restructure the specified code files: rearrange module layout, reorder logically, extract shared logic, remove redundancy, add section separators and comments. **Must not change any functionality or logic.**

## Target Scope

`$ARGUMENTS` specifies the target:
- **File path**: `/reorganize src/engine.go` — reorganize a single file
- **Directory path**: `/reorganize src/core/` — reorganize all code files in the directory
- **`diff`**: `/reorganize diff` — reorganize all uncommitted files (via `git diff --name-only --diff-filter=d HEAD`)

If no argument is provided, prompt the user to specify a target.

## Execution Flow

### Step 1: Determine File List

Resolve the file list from `$ARGUMENTS`. For directories, recursively collect all code files (excluding vendor, node_modules, generated files, etc.). For `diff`, collect all uncommitted modified files (staged + unstaged).

Output:

```
REORGANIZE
  Target:    {file/dir/diff}
  Files:     {N} files
  {file list}
```

### Step 2: Analyze and Reorganize Each File

Process files one at a time. Complete each file before moving to the next.

#### 2.1 Read and Analyze Current Structure

Read the entire file and analyze:
- All type definitions, constants, variables, functions/methods present
- Dependencies and relationships between sections
- Duplicate or reusable logic patterns within this file
- Whether the file couples multiple unrelated functional modules

#### 2.2 Decide Whether to Split the File

Split into multiple files when the file couples multiple clearly distinct functional modules, making it hard to read and maintain:
- The decision is based on module coupling, not line count
- Each resulting file should have focused, single responsibility

When splitting:
- Name new files clearly to reflect their purpose (e.g., `engine.go` -> `engine.go` + `engine_strategies.go` + `engine_signals.go`)
- Keep the same package/module, ensure compilation passes
- Place shared type definitions in the most logical file, or a separate types file

If the file is already focused and coherent, do not split.

After splitting, apply steps 2.3-2.7 to each resulting file.

**Import fixing**: After splitting, scan the project for files that import or reference the original file. If the split moved symbols to new files, update the affected imports. This may require modifying files outside the original target scope — this is the only case where that is allowed.

#### 2.3 Restructure File Layout

Organize code blocks in a logical order based on the file's actual content. Common reference (adapt as needed):

```
- package/module declaration, imports
- constants, variables
- type definitions
- constructors
- core business methods
- helper / utility methods
```

Core principle: **a reader scanning top-to-bottom should naturally understand the file's structure and business flow**. Keep related code together; high-level logic before implementation details.

Separate functional sections with divider comments. First check if the project already uses a divider style (scan existing files for patterns like `// ---`, `// ===`, `#region`, etc.) and adopt that style. If no existing convention is found, use a simple comment divider appropriate for the language.

#### 2.4 Extract Reusable Logic

Check for duplicate code patterns **within the file being processed** (do not modify files outside the target scope):
- 2+ occurrences of logic spanning 5+ lines -> extract into a standalone helper
- 3+ occurrences regardless of length -> extract into a standalone helper
- Name extracted helpers clearly, place them in the helper/utility section

#### 2.5 Remove Redundancy

- Delete unused functions, types, constants, variables
- Delete duplicate implementations (keep the better one)
- Delete meaningless comments (e.g., empty `// TODO`, placeholder `// xxx`)
- **Do not delete** comments with business meaning or doc comments

#### 2.6 Add Comments

- Section divider comments for each functional area
- Concise comment before core business methods (one line, explain what it does)
- Concise comment before type definitions
- No comments needed for utility methods or self-explanatory code
- Do not translate existing comments -- leave them as-is

#### 2.7 Verify

Run compilation and test checks on modified files. Detect build/test commands from the project:
- Go: `go build ./...` then `go test ./...`
- Rust: `cargo check` then `cargo test`
- TypeScript: `tsc --noEmit` then `npm test`
- Python: `python -m py_compile` then `pytest`
- Other: language-appropriate compile check then test suite

If any check fails, fix the issue and re-check. Max 3 attempts.

### Step 3: Output Report

After each file is processed, briefly report what was done and the build/test result. After all files, output summary:

```
REORGANIZE COMPLETE
  Files processed:    {N}
  Files split:        {list, or "none"}
  Build:              {PASS / FAIL}
  Tests:              {PASS / FAIL / no tests found}
```

## Constraints

- **Preserve functionality and logic** -- this is restructuring, not rewriting. Input/output behavior must remain identical.
- **Preserve public API** -- exported functions, methods, and type signatures must not change.
- **Preserve tests** -- if corresponding test files exist, all tests must pass after reorganization.
- **Conservative splitting** -- only split when a file truly couples multiple distinct modules causing confusion.
- **Conservative extraction** -- only extract when there is genuine multi-site duplication with meaningful complexity.
