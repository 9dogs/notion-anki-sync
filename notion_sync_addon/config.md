# Configuration

- `debug: bool [default: false]` - enable debug logging to file
- `sync_every_minutes: int, default [30]` - auto sync interval in minutes. Set to 0 to disable
- `anki_target_deck: str [default: "Notion Sync"]` - the deck loaded notes will be added to
- `notion_token: str [default: None]` - Notion APIv2 token (see below)
- `notion_namespace: str [default: None]` - Notion namespace (your username) to form source URLs
- `notion_pages: array [default: [] ]` - List of Notion pages to export notes from (see below)

## Notion token

To get **Notion API token** log in to Notion via a browser (assuming Chrome here),
then press `Ctrl+Shift+I` to open Developer Tools, go to the "Application" tab
and find `token_v2` under Cookie on the left.

## Notion pages
`notion_pages` should look like that:
```json
...
"notion_pages": [
    {
      "page_id": "<page_id1>",
      "recursive": false
    },
    {
      "page_id": "<page_id2>",
      "recursive": true
    }
  ]
```
To get **Notion page id** open up the page in a browser and look at the
address bar. 32 chars of gibberish after a page title is the page id:
`https://www.notion.so/notion_user/My-Learning-Book-8a775ee482ab43732abc9319add819c5`
➡ `8a775ee482ab43732abc9319add819c5`
Parameter `recursice` indicates whether page should include its subpages.
