"""Tool schemas the model can call: one browser action per step."""

TOOLS: list[dict] = [
    {
        "name": "plan",
        "description": (
            "Record your plan. REQUIRED before your first browser action, and call it again when you reach a new kind "
            "of page (listing, filters, form, results) or when the plan changes. Study the page's controls first: say "
            "what the page offers (filters, sorts, menus, forms), which of them map to parts of the goal, and the steps "
            "you will take. The checklist is the set of concrete, verifiable criteria that must be true when you are "
            "done; `done` is rejected while items are unmarked. Mark items you have verified with `completed`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_summary": {"type": "string", "description": "What kind of page this is and what it offers."},
                "controls_to_use": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Controls on this page that map to the goal, e.g. 'Finder Score filter: 9+ Excellent'.",
                },
                "steps": {"type": "array", "items": {"type": "string"}, "description": "Intended sequence of actions."},
                "checklist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Goal criteria to satisfy (replaces the current checklist). Required on the first plan.",
                },
                "completed": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "1-based checklist numbers you have now verified as satisfied.",
                },
            },
            "required": ["page_summary", "steps"],
        },
    },
    {
        "name": "click",
        "description": "Click an element by its [id] from the latest page observation (link, button, tab, checkbox, ...).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Element id from the latest observation."}},
            "required": ["id"],
        },
    },
    {
        "name": "type",
        "description": (
            "Type text into an input/textarea (replaces the current value). Never for elements marked "
            "SENSITIVE (passwords, card numbers, one-time codes). Set submit=true to press Enter afterwards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "text": {"type": "string"},
                "submit": {"type": "boolean", "description": "Press Enter after typing (e.g. to run a search)."},
            },
            "required": ["id", "text"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option (by visible text) in a <select> dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "option": {"type": "string"}},
            "required": ["id", "option"],
        },
    },
    {
        "name": "press",
        "description": "Press a keyboard key, e.g. Enter, Escape, Tab, ArrowDown.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "scroll",
        "description": "Scroll the page to reveal more content (new elements may appear in the next observation).",
        "input_schema": {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["down", "up"]}},
            "required": ["direction"],
        },
    },
    {
        "name": "goto",
        "description": "Navigate to a URL on the allowed site (absolute http(s) URL).",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "back",
        "description": "Go back to the previous page.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait",
        "description": "Wait for the page to finish loading or updating (max 10 seconds).",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "run_playwright",
        "description": (
            "Write and run a short Playwright (sync, Python) snippet against the current page, for things the simple "
            "tools cannot do: hover menus, widgets that need several coordinated actions, drag, extracting data from "
            "many elements, robust selectors. Available: `page` (Playwright Page, guarded), `nav(id)` (locator for an "
            "element id from the latest observation), `re`, `json`, `print()`. Prefer role/label/text locators "
            "(page.get_by_role('checkbox', name='No annual fee').check()). Set a variable `result` or print() to return "
            "data. No imports, no while-loops; keep it to one logical step. Guards still apply: password/card fields "
            "are refused, submit/pay/send-like clicks need approval (blocked in dry run), goto only to seen links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python statements using `page` / `nav`."},
                "reason": {"type": "string", "description": "One sentence: what this achieves."},
            },
            "required": ["code", "reason"],
        },
    },
    {
        "name": "ask_human",
        "description": (
            "Ask the person a question when you need information that is not in the task inputs "
            "(a preference, a missing detail). Never ask for passwords, card numbers or codes - "
            "sign-in is handled automatically by pausing for the person."
        ),
        "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        "name": "done",
        "description": "Finish the task. Call this when the goal is achieved, or when it cannot be achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "summary": {"type": "string", "description": "What was done / why it failed."},
                "result": {
                    "type": "object",
                    "description": "The requested output as a JSON object (e.g. products found, quote details).",
                },
            },
            "required": ["success", "summary"],
        },
    },
]
