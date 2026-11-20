"""The two bots side by side: an old OneBot bot (a logged-in QQ account) in the same process as the official one.

Everything in this folder exists for that time only: the old bot as the ears, groups and members
recognised from what both bots hear, the handshake, migration codes, what the old
bot may still send, and keeping its member lists. With no OneBot bot connected none of it does
anything, and the day no deployment runs the two together the folder can go as a whole (what
the rest of the bridge takes from it is imported at the top of the package's __init__.py).

Moving over, with the OneBot bot still running in the same process:

- A group the official bot has joined starts in *shadow*: it only answers there when mentioned,
  so the two bots never answer the same command twice. The group is tied to the old group's
  number (the group plugins then find their settings again) by itself, when the bot was put
  there by a SUPERUSER or by the old bot's own account and three messages have been heard by
  both bots, or at once by the handshake described below.
- A member's OpenID is tied to their QQ number either by itself, when the same message is heard
  by both bots in a tied group twice (same text, seconds apart, nobody else saying it), or by
  hand: `/迁移` to the official bot gives a code to send to the old bot from the old account.
  From then on the member is their QQ number again: bindings, records and permissions apply.
- In a tied group with both bots the old bot is the ears and the official bot the mouth
  (OFFICIAL_BRIDGE_EARS, default true): what the old bot hears there is handed to the bridge
  bot as its own event, with the members' real numbers, handled by the plugins once and
  answered through the official API; the old bot itself answers nothing there. This does not
  depend on the group letting the official bot receive every message, nor on members being
  tied to their numbers. The official platform's own copies of those messages are only used to
  reply to (a quoted reply instead of an unprompted message) and to learn who is who.
  With OFFICIAL_BRIDGE_EARS=false the rule below applies instead.
- Then, in a tied group with both bots the official bot answers everyone and the old bot nothing (it
  stays to listen, so that members can be recognised, and still takes `/迁移` codes). A member
  who is not tied yet is recognised from their very first message there: the official bot holds
  it for a moment until the old bot has heard the same text at the same time from one member
  with the same name, and answers it as that QQ number, so nobody is told they are not bound.
  OFFICIAL_BRIDGE_TAKEOVER=members is the gentler variant: the official bot answers only the
  members already tied, the old bot the others.
- A group is tied at once by a handshake: when the official bot is put in a group the old bot
  sees a robot account join one of its groups, and says there, to that account, a line with a
  one-time code. The official bot hearing that line from the old bot's account knows which
  group it is in (and the old bot knows the official bot's number).
  The old client does not report every such join,
  so once the official bot's number is known (learnt, or given as OFFICIAL_BRIDGE_UINS) the old
  bot also looks for itself: after a join the official bot reports, and when either bot
  connects, it asks in which of its groups not tied yet that account is a member, and shakes
  hands there. This line is the one thing the old bot still says of its own accord.
- The two bots never act on each other: the official bot drops what the old bot's account says,
  and the old bot drops what the official bot says and what is addressed to it by a mention. The
  number the official bot has on the old bot's side is learnt the first time the old bot hears,
  from an account QQ marks as a robot, a text the official bot has just sent to that group.
- The old bot sends nothing on the bridge bot's behalf (OFFICIAL_BRIDGE_LEGACY_SENDS, default
  false): what the official bot cannot send is not sent. A push goes to
  the groups the official bot is in and is allowed to speak in, and nowhere else; its group
  list is its own. The old bot is still asked what only a QQ client knows (members, names,
  essence messages). With the setting true, sends the official bot cannot make are passed to
  the old bot instead.

By file: presence.py (which groups the old bot is in, which of the two answers), sends.py (what
the old bot still sends and which calls it carries out), hearing.py (recognising groups and
members from what both hear), ears.py (the old bot's hearing handed to the bridge bot),
handshake.py, migrate.py (/迁移 codes), rosters.py (keeping the old bot's member lists).
"""

from . import ears, handshake, hearing, migrate, presence, rosters, sends  # noqa: F401
