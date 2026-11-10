// telegram-agent-computer: the dashboard loads every plugin's entry. This plugin has no dashboard tab
// (hidden); its page is the Telegram Mini App at /dashboard-plugins/telegram-agent-computer/index.html.
(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  var React = SDK.React;
  window.__HERMES_PLUGINS__.register("telegram-agent-computer", function AgentComputer() {
    return React.createElement("p", { style: { padding: 16 } },
      "Agent computer is a Telegram Mini App: open it from the Computer card in Telegram.");
  });
})();
