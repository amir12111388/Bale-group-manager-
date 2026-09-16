# CUSTOMIZATION_CHANGELOG

## Added

- PM-only customization panel for each group.
- Group-specific message templates for moderation actions.
- Group-specific message templates for lock violations.
- Group-specific custom command aliases.
- Ready-made tone packs: friendly, fun, serious.
- Deep link support: `start=custom_<group_id>`.
- Group panel entry button: `🎨 شخصی‌سازی`.
- Private main menu shortcut: `🎨 شخصی‌سازی گروه`.

## Safety

- Customization is not editable from group chat.
- Only bot owners, group admins, or internal group managers can edit a group's customization.
- Bad template formatting does not crash the bot; the raw text is sent instead.
- Custom text length is limited to 1200 characters.
