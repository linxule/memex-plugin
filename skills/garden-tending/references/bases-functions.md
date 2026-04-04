# Obsidian Bases — Formula Function Reference

> Upstream source: [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) (absorbed 2026-04-03)
> Re-check upstream periodically for new functions or breaking changes.

Reference for formulas in `.base` files. Use when creating or modifying `_views/*.base` files during garden-tending.

## Critical Gotcha: Duration Type

Subtracting two dates returns a **Duration**, not a number. You must access a numeric field (`.days`, `.hours`, etc.) before applying math functions.

```yaml
# Correct
"(date(due_date) - today()).days"              # Returns number
"(date(due_date) - today()).days.round(0)"     # Rounded number
"(now() - file.ctime).hours.round(0)"          # Hours since created

# Wrong — Duration doesn't support .round() directly
"((date(due) - today()) / 86400000).round(0)"  # Error
```

## Quoting Rule

Wrap formulas containing double quotes in single quotes in YAML:

```yaml
formulas:
  status_label: '"active"'                          # Literal string
  priority: 'if(status == "urgent", "high", "low")' # Contains double quotes
  days_old: "(now() - file.ctime).days"              # No double quotes, either quote style works
```

---

## Global Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `if()` | `if(cond, true, false?)` | Conditional |
| `now()` | `now(): date` | Current date+time |
| `today()` | `today(): date` | Current date (00:00:00) |
| `date()` | `date(string): date` | Parse `YYYY-MM-DD HH:mm:ss` |
| `duration()` | `duration(string): duration` | Parse duration string |
| `min()` | `min(n1, n2, ...)` | Smallest number |
| `max()` | `max(n1, n2, ...)` | Largest number |
| `number()` | `number(any)` | Convert to number |
| `link()` | `link(path, display?)` | Create a link |
| `list()` | `list(element)` | Wrap in list if not already |
| `file()` | `file(path)` | Get file object |
| `image()` | `image(path)` | Create image for rendering |
| `icon()` | `icon(name)` | Lucide icon by name |
| `html()` | `html(string)` | Render as HTML |
| `escapeHTML()` | `escapeHTML(string)` | Escape HTML chars |

## Date Functions

**Fields:** `date.year`, `.month`, `.day`, `.hour`, `.minute`, `.second`, `.millisecond`

| Function | Description |
|----------|-------------|
| `date.date()` | Remove time portion |
| `date.format(pattern)` | Format with Moment.js pattern |
| `date.time()` | Get time as string |
| `date.relative()` | Human-readable relative time |
| `date.isEmpty()` | Always false for dates |

### Date Arithmetic

```yaml
"date + \"1M\""              # Add 1 month
"date - \"2h\""              # Subtract 2 hours
"now() + \"1 day\""          # Tomorrow
"today() + \"7d\""           # A week from today
```

Duration units: `y/year/years`, `M/month/months`, `w/week/weeks`, `d/day/days`, `h/hour/hours`, `m/minute/minutes`, `s/second/seconds`

## Duration Fields

| Field | Type | Description |
|-------|------|-------------|
| `duration.days` | Number | Total days |
| `duration.hours` | Number | Total hours |
| `duration.minutes` | Number | Total minutes |
| `duration.seconds` | Number | Total seconds |
| `duration.milliseconds` | Number | Total milliseconds |

Access a field before applying number functions like `.round()`, `.floor()`, `.ceil()`.

## String Functions

**Field:** `string.length`

| Function | Description |
|----------|-------------|
| `contains(value)` | Check substring |
| `containsAll(...values)` | All substrings present |
| `containsAny(...values)` | Any substring present |
| `startsWith(query)` | Starts with query |
| `endsWith(query)` | Ends with query |
| `isEmpty()` | Empty or not present |
| `lower()` | To lowercase |
| `title()` | To Title Case |
| `trim()` | Remove whitespace |
| `replace(pattern, repl)` | Replace pattern |
| `repeat(count)` | Repeat string |
| `reverse()` | Reverse string |
| `slice(start, end?)` | Substring |
| `split(sep, n?)` | Split to list |

## Number Functions

| Function | Description |
|----------|-------------|
| `abs()` | Absolute value |
| `ceil()` | Round up |
| `floor()` | Round down |
| `round(digits?)` | Round to digits |
| `toFixed(precision)` | Fixed-point string |
| `isEmpty()` | Not present |

## List Functions

**Field:** `list.length`

| Function | Description |
|----------|-------------|
| `contains(value)` | Element exists |
| `containsAll(...values)` | All elements exist |
| `containsAny(...values)` | Any element exists |
| `filter(expr)` | Filter by condition (uses `value`, `index`) |
| `map(expr)` | Transform elements (uses `value`, `index`) |
| `reduce(expr, init)` | Reduce to value (uses `value`, `index`, `acc`) |
| `flat()` | Flatten nested lists |
| `join(sep)` | Join to string |
| `reverse()` | Reverse order |
| `slice(start, end?)` | Sublist |
| `sort()` | Sort ascending |
| `unique()` | Remove duplicates |
| `isEmpty()` | No elements |

## File Functions & Fields

**Fields:** `file.name`, `file.path`, `file.ctime`, `file.mtime`, `file.size`, `file.ext`, `file.folder`

| Function | Description |
|----------|-------------|
| `asLink(display?)` | Convert to link |
| `hasLink(otherFile)` | Has link to file |
| `hasTag(...tags)` | Has any of the tags |
| `hasProperty(name)` | Has property |
| `inFolder(folder)` | In folder or subfolder |

## Link Functions

| Function | Description |
|----------|-------------|
| `asFile()` | Get file object |
| `linksTo(file)` | Links to file |

## Any Type

| Function | Description |
|----------|-------------|
| `isTruthy()` | Coerce to boolean |
| `isType(type)` | Check type |
| `toString()` | Convert to string |

## Guard Empty Properties

Always guard potentially empty properties with `if()`:

```yaml
'if(status.isEmpty(), "unknown", status)'
'if(due_date.isEmpty(), "no date", date(due_date).format("MMM D"))'
```
