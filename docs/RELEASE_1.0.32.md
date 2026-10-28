# PNUT M&M 1.0.32

- Passive party notices such as **Your party member Friend has been slain by
  Guard Nicolas!** count as a party death. They no longer add a kill or identify
  the NPC killer as a party member. Active party kills retain their existing behavior.
- Self-only Session views omit nearby players' deaths. Group and nearby-player
  views retain their separate counts.
- Feed and Revenge omit stale fight headers and fight copy, history and respawn
  actions. Combat tabs retain those actions, and Session retains its own header.
- The self-view fight header shows zero damage and DPS when you have no recorded
  activity in that fight and matches your own row regardless of name capitalization.
