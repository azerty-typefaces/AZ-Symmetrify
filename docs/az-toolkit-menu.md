# AZ Toolkit menu tags

AZ plugins share **Window → AZ Toolkit**. Menu items are identified by stable
integer tags, assigned with `setTag_()` and retrieved with `itemWithTag_()`
(Objective-C: `itemForTag:`). Titles are display labels, not identifiers.

| Item | Constant | Tag | Mnemonic |
| --- | --- | --- | --- |
| Shared AZ Toolkit parent | `AZ_TOOLKIT_MENU_TAG` | `0x415A544B` | `AZTK` |
| Letter Digger child | `AZ_LETTER_DIGGER_MENU_TAG` | `0x415A4C44` | `AZLD` |
| Symmetrify child | `AZ_SYMMETRIFY_MENU_TAG` | `0x415A5359` | `AZSY` |

Every AZ plugin must use the same parent tag. Give each plugin child a distinct,
stable tag; record new assignments in this table when porting plugins. The
mnemonics encode four ASCII characters as a hexadecimal integer. Reserve `AZTK`
for the parent, `AZLD` for Letter Digger, and `AZSY` for Symmetrify.

Shadow currently registers only a reporter and palette, with no AZ Toolkit menu
child, so it has no child tag assigned.

## Find or create

Keep menu creation in the plugin class and call it from `start()`:

```python
window_menu = Glyphs.menu[WINDOW_MENU].submenu()
parent = window_menu.itemWithTag_(AZ_TOOLKIT_MENU_TAG)
if parent is None:
    # Create the AZ Toolkit parent and submenu, tag the parent, then add it.
    # Add the Window-menu separator and submenu footer only in this branch.
    ...

submenu = parent.submenu()
item = submenu.itemWithTag_(AZ_LETTER_DIGGER_MENU_TAG)
if item is not None:
    return  # Reuse the existing item in place.

# Create, tag and insert the plugin child only when it is missing.
```

Insert plugin children alphabetically, without their `AZ ` prefix, above the
trailing separator and **More AZ plugins…** link. Iteration is only needed to
choose the insertion point; use tags for lookup. Never remove and recreate an
existing tagged item.

The plugin that creates the parent also creates the website link, targeting its
own `openAZSite_` method with `setTarget_(self)`. The action opens
`https://azerty-typefaces.com`. Plugin children also target their own plugin
instance. No separate target class or retention store is
needed: `.glyphsPlugin` and `.glyphsFilter` instances persist for the app session.
Do not assume this lifetime for other plugin types; menu items do not retain
their targets.

## Porting

Use [Letter Digger's implementation](../AZ-LetterDigger.glyphsPlugin/Contents/Resources/plugin.py)
as the reference. Copy the shared parent tag, assign the plugin's own child tag,
and replace title-based lookup and separate website targets with this pattern.
Update all participating plugins and restart Glyphs to rebuild the menus;
older, untagged parent items cannot be found by the new lookup.
