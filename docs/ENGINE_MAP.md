# FM27 engine map (reverse engineering)

How FM27's interface talks to its native engine, extracted offline from the game files with
`tools/re/fm_native.py` (no game needed, read-only). Generated tables:

- `docs/engine_handlers.json` — every engine-side UI handler (533) with the 4-character event codes and
  data property codes its code uses;
- `docs/engine_actions.json` — every action class (229) with its candidate action codes.

## Method

The native engine (`fm.app/Contents/PlugIns/game_plugin.bundle`, arm64 slice) is stripped, but:

1. `LC_FUNCTION_STARTS` still lists every function (117,968).
2. C++ RTTI type names survive (`N11game_plugin…E`); with chained-fixup pointers decoded by their low
   36 bits, typeinfo objects and vtables are found (5,605 classes), so functions can be named after the
   class whose vtable holds them.
3. FM's identifiers are 4-character codes (properties like `aroM` morale, events like `BJ78` press answer).
   They are built in code with `MOVZ` + `MOVK` on one register; scanning for those gives every code and the
   functions using it (24,916 codes).

Cross-checks against recorded traffic: press conference events `BJ78` (answer), `IXN1` (storm out),
`RNQw` (no comment), `u4v3` (start) all live in `PRESS_CONFERENCE_EDIT_SESSION_SPEAK_TO_UI_HANDLER`;
the editor session event `YStb` in `EDITOR_UI_HANDLER`; morale `aroM` in `PERSON_UI_HANDLER`; the team
morale edit's action code `LYnC` in `TEAM_CHANGE_ALL_MORALE_ACTION`.

## Actions

Performing an action is the generic event 44 sent to an action object target
(`' 011'=1, Guid, dica=<action code>`), with a data record of context and parameters
(`aetT` team, `muhH` human manager, `ferd` record, `Lerd` [records], `aCPN`, plus action fields such as
`ulav` value). Recorded example (team morale to 18): `dica='LYnC'`, data `{aetT, aCPN, muhH, ferd, Lerd, ulav:18}`.

Key actions and codes (from `engine_actions.json`):

| Action | Code | Natural consequence (computed by FM) |
| --- | --- | --- |
| PRAISE_PLAYER_PERFORMANCE | DMmZ | morale, happiness, view of manager |
| PRAISE_PLAYER_TRAINING_DEVELOPMENT | D2Rk | morale; development |
| PRAISE_PLAYER_CONDUCT / INTERNATIONAL / LOAN_SPELL | see json | |
| WARN_PLAYER_CONDUCT | EUgd | pressure, relationship |
| WARN_PLAYER_PERFORMANCE / TRAINING_DEVELOPMENT | see json | |
| DISCIPLINE_PLAYER | `M9 z` (fields aarf, tmAF) | discipline, relationship |
| PLAYER_CONCERNED | Q5QD | happiness, trust |
| TEAM_HOLD_TEAM_MEETING | x6It | dressing-room morale |
| TEAM_SET_RECOMMENDED_MENTORING_GROUPS | BR1p | personality / hidden attributes over time (mentoring) |
| PLAYER_TRAINING_FOCUS | xkAl (fields tpti, fwti, fwtc) | attribute development |
| MAKE_PLAYER_CAPTAIN | ` hjP` | leadership |
| TEAM_CHANGE_ALL_MORALE | LYnC | morale (IGE) |
| PLAYER_REMOVE_ALL_UNHAPPINESS | O30E | happiness (IGE) |

FM's natural feedback lives in the native `MORALE_MANAGER` and the interaction outcome classes
(`INTERACTION_SUMMARY_MORALE_CHANGE_OBSERVATION`, `…_PLAYER_HAPPINESS_CHANGE/ADDITION/REMOVAL/ESCALATION…`,
`…_RELATIONSHIP_CHANGE_OBSERVATION`). Personality changes come through mentoring
(`MENTORING_GROUP_UI_HANDLER`, `PLAYER_PERSONALITY_CHANGE_ADVICE_SUGGESTION`).

## Status

- Mapped: handlers, events, properties, actions (tables above).
- Not yet verified live: calling a player action (e.g. D2Rk on one player) with event 44; the exact data
  record per action (fields per handler are in `engine_handlers.json`).
