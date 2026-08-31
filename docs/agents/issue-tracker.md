# Issue tracker: GitHub

Issues and PRDs for this repository live as GitHub Issues. Use the `gh` CLI for issue operations and infer the repository from the current clone.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read with comments: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels`
- Comment: `gh issue comment <number> --body "..."`
- Add or remove labels: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- Close with a summary: `gh issue close <number> --comment "..."`

For a long issue body, write the proposed text to a reviewed temporary Markdown file and pass it with `--body-file`.

## Skill vocabulary

- “Publish to the issue tracker” means creating a GitHub Issue.
- “Fetch the relevant ticket” means reading the issue body, comments, and labels.
- Link each implementation change and its test evidence to the relevant Issue when practical.
