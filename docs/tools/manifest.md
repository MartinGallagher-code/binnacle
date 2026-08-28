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

## Selectors

Each argument is one selector, and they are **ANDed** — an element has to
satisfy all of them. Inside one selector, top-level commas are **OR**.

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

## One rule about containers

**A selector names elements; the answer is the machines at or under them.**

That is the whole of it, and it is why `rack[1-3]` works. A rack is not a
server, so matching the three rack elements and then filtering for
`role=server` would leave nothing at all. Instead each machine is tested
against the selector and then against every ancestor it has, so naming a
container names its contents.

The same rule is why `u=42` — an attribute only racks carry — answers with
the machines in the 42U racks rather than with nothing. It is one rule, not
a special case for kinds.

## Forgiving ids

A rack called `R01` is the rack you mean when you type `rack[1]`, and having
to know the zero padding would make the tool useless from memory. Ids are
matched exact first, then numerically with padding and any leading letters
ignored, then as a glob.

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

## What counts as a server

`--role` decides, defaulting to `server`, and `--role any` turns the filter
off. A layout that sets no `role=` anywhere gets the leaf nodes — the things
with nothing inside them — and is told so on stderr, because silently
guessing is how a fan-out ends up including the switches.

```bash
manifest floor.dc 'rack[1]' --role tor      # the switches instead
manifest floor.dc 'rack[1]' --role any      # everything in the rack
```

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
many matched and nothing else.

## Exit status

| Code | Meaning |
|---|---|
| `0` | at least one server matched |
| `1` | the selectors matched nothing, or matched no servers |
| `2` | usage error, or the layout could not be read |

Exit 1 is a finding rather than a crash: an empty fleet list is exactly the
thing you want a script to stop on rather than fan out to nobody.
