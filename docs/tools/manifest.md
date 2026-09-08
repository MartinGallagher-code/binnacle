# `manifest`

**Which servers are those, in the layout?**

A ship's manifest is the list of what is actually aboard. This is that list
for a floor of a datacenter: you name a place — a rack, a row, a room — and
it prints the machines, one per line.

```bash
manifest floor.dc 'rack[1-3]'          # every server in racks 1 to 3
manifest floor.dc 'row[A,C]' +gpu      # the gpu servers in two rows
manifest floor.dc 'room[1]' --csv      # the same, with where each one is
manifest floor.dc r01u05,r03u17        # those two, by name
```

## How a question is answered

Three rules, and they are the whole tool:

1. **The layout is a tree** — `dc → room → row → rack → node`.
2. **A selector matches any element in it**, not just machines.
3. **The answer is the machines at or under whatever matched.**

Rule 3 is the one that has to be believed before the rest makes sense. A
rack is not a server, so `rack[1-3]` cannot mean "rack elements, filtered to
role=server" — that would be nothing at all. Each machine is tested against
the selector and then against every ancestor it has, so **naming a container
names its contents**. The same rule is why `u=42` — an attribute only racks
carry — answers with the machines in the 42U racks rather than with nothing.
It is one rule, not a special case per kind.

Several arguments are **ANDed**: a machine has to satisfy all of them, each
by itself or by an ancestor. Inside one argument, top-level commas are
**OR**. `--role` (default `server`) then decides which of the matched
elements count as machines worth printing.

## A worked example

Everything below runs against the layout `--sample` prints, so you can
follow it with no floor plan of your own. It is 373 elements: 328 servers
and 18 switches across two halls and a GPU room.

```bash
manifest --sample > floor.dc

manifest floor.dc --count                    # 328  every server
manifest floor.dc 'row[A]' --count           # 168  row A, in all three rooms
manifest floor.dc 'rack[1-4]' --count        # 168  the same machines, named by rack
manifest floor.dc 'room[gpu1]' --count       #   8  one room
manifest floor.dc +gpu --count               #   8  the same, by inherited tag
manifest floor.dc 'model=hgx*' --count       #   8  and again, by attribute
manifest floor.dc --role tor --count         #  18  the switches instead
```

Three of those are the same eight machines asked for three different ways —
by container, by tag and by attribute — which is the shape of most real
questions.

## The problem it solves

A fleet list is a datacenter fact — these racks, in that room, hold these
machines — but it is almost always kept as a text file somebody maintains by
hand. So it is always slightly wrong: the four machines that were
decommissioned last quarter are still in it, the row that was added in March
is not, and nobody finds out until a fan-out quietly skips a rack.

The building already knows. Rooms hold rows, rows hold racks, racks hold
machines, and if that description exists as a file then the host list is a
query against it rather than a second copy of it that has to be kept in step.

`manifest` is that query. It reads the layout and prints names, which is the
shape every other tool here already takes:

```bash
manifest floor.dc 'rack[1-3]' | muster add -
manifest floor.dc 'row[A,C]' > hosts.txt && agree --hosts hosts.txt
manifest floor.dc 'room[1]' | netmesh gen --servers -
```

It reads and prints. Nothing is contacted — no ssh, no DNS, no inventory
API — nothing is written, and the layout file is never modified.

## Selectors

| Selector | Means |
|---|---|
| `rack[1-3]` | kind and id: racks 1, 2 and 3 |
| `row[A,C]` | rows A and C |
| `room[1]` | room 1 |
| `r01u05,r01u07` | either of those two, by name |
| `+gpu` | carries the tag, inherited ones included |
| `role=tor` | an attribute; globs allowed, so `model=r76*` |
| `DH1/A/R01/u05` | a path, or any suffix of one |
| `wr01r03*` | a glob against name, id or path |
| `!+decom` | negated |

```bash
manifest floor.dc 'room[1]' 'row[A]' '!+decom'
```

Ids are matched forgivingly, because a rack called `R01` is the rack you
mean when you type `rack[1]`, and having to know the zero padding would make
the tool useless from memory. Exact first, then numerically with padding and
any leading letters ignored, then as a glob.

Both range spellings work — `rack[1-3]` as the rest of this package writes a
range, and `rack[1..3]` as the layout file does — because a reader should not
have to know which side of that fence they are standing on. A plural kind is
accepted where the singular exists, so `racks[1-3]` and `rack[1-3]` are the
same question.

A kind the layout has never heard of is named rather than left as an empty
answer, because *nothing matched* sends you off checking your rack numbers
when the problem was the word in front of the bracket:

```text
[manifest] no cage in floor.dc -- it has dc, node, rack, room, row
```

It is a report rather than a refusal, since one branch of an OR may
legitimately name a kind this particular floor does not have.

## When the count is not what you expected

Forgiving ids have one cost, and it is the single most surprising thing
about this tool: **a bare number ignores the letters in front of an id.**
Against the sample floor, `rack[1]` is `r01` in each hall *and* `g01` in the
GPU room, and `room[1]` is `wr01` *and* `gpu1` — `gpu1` ends in a 1, so as
far as the matcher is concerned it is room 1.

That is said out loud when it happens:

```text
$ manifest floor.dc 'rack[1]' --count
[manifest] rack[1] matched more than one spelling of that id (g01, r01) --
a bare number ignores the letters in front of it; rack[g01] or rack[r01],
or a path selector, picks one out
44
```

It stays a report rather than a refusal — one number answering to two
spellings is often exactly what you meant — but you are told, because
quietly returning a second room's worth of machines is how a fan-out reaches
a rack nobody meant to touch. Two ways to mean one of them:

```bash
manifest floor.dc 'rack[r01]' --count      # 40  spell the letters
manifest floor.dc wr01/A/r01 --count       # 20  or give a path
```

### `--explain`

For the general form of the question — *why is this count what it is?* —
`--explain` prints, per selector, the elements it actually named and how
many were still standing after it. It goes to stderr, so the host list still
pipes.

```text
$ manifest floor.dc 'room[1]' 'row[A]' --explain --count
[manifest] explain: floor.dc, 373 element(s), role=server
  1. room[1]
       names 2 element(s); 373 -> 193 element(s)
       room  wr01     IAD1/wr01   <- by number, not by name
       room  gpu1     IAD1/gpu1   <- by number, not by name
  2. row[A]
       names 2 element(s); 193 -> 102 element(s)
       row   A        IAD1/wr01/A
       row   A        IAD1/gpu1/A
  = 88 server(s)
88
```

The `<-` marks an id that answered to a bare number rather than to its own
name, which is where the extra room came from. A selector that narrowed the
answer to nothing says so on its own line, which is the whole question when
a fan-out comes back with no hosts and three selectors to blame.

It is asked for outright, so `--quiet` does not silence it.

## Starting from nothing

`--sample` prints a commented layout that uses every construct this tool
understands, so it is both the fastest way to see the format and a
reasonable file to edit into your own floor:

```bash
manifest --sample > floor.dc
manifest floor.dc 'rack[1-4]'
```

It needs no layout of its own — that is the point of it — and it writes
only the file, so the summary line stays on stderr where a redirect cannot
catch it. The sample describes two halls and a GPU room: 328 servers and 18
switches, with the counts stated in its own footer so you can check the
tool agrees with the file.

## The layout file

The `.dc` format from the
[layout_visualizer](https://github.com/MartinGallagher-code/layout_visualizer)
project, which is the format's reference implementation. Indentation nests,
ranges expand, and one line describes a whole floor:

```text
dc IAD1
  room DH1
    row A..D
      rack R[01..06] u=42
        node tor at=42 role=tor +switch
        node u[01..20] role=server +x86 model=r7625
```

Five lines, 504 elements. `name={room}{rack}{id}` gives every machine a flat
hostname like `wr12r06u15`, and where a name is set that is what gets
printed.

Attributes and tags **inherit downward**, so a `+prod` on the room is carried
by every machine under it. A handful do not, because they describe one
element rather than a class of them: a rack's `u=42` is its own height, not
every machine's, and the `name=` on the `dc` line is the site's name, not
forty-eight machines all called Site.

What is understood here is the element half of the format. `net` and `link`
lines describe cabling, not machines, and are read past.

## What counts as a server

`--role` decides, defaulting to `server`, and `--role any` turns the filter
off. A layout that sets no `role=` anywhere gets the leaf nodes — the things
with nothing inside them — and is told so on stderr, because silently
guessing is how a fan-out ends up including the switches.

```bash
manifest floor.dc 'rack[1]' --role tor      # the switches instead
manifest floor.dc 'rack[1]' --role any      # everything in the rack
```

`--role any` is also the quickest way to see the containers a selector
picked out, since it stops filtering them away.

## Where each one is

```bash
$ manifest floor.dc 'rack[1]' --csv
name,path,kind,role,room,row,rack,slot
wr01r01u01,FLAT/wr01/A/r01/u01,node,server,wr01,A,r01,u01
wr01r01u02,FLAT/wr01/A/r01/u02,node,server,wr01,A,r01,u02
```

`slot` is the `at=` where the layout pins a node, and the node's own id
otherwise — a layout that names its machines `u01..u40` has already said
where they are, and an empty column for every machine in the fleet would be
the wrong answer to give.

`--path` prints the full path instead of the name, and `--count` prints how
many matched and nothing else. `--sample` writes the example layout above.

## Exit status

| Code | Meaning |
|---|---|
| `0` | at least one server matched |
| `1` | the selectors matched nothing, or matched no servers |
| `2` | usage error, or the layout could not be read |

Exit 1 is a finding rather than a crash: an empty fleet list is exactly the
thing you want a script to stop on rather than fan out to nobody.
