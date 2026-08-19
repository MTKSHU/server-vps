import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "./styles/app.css";
import App from "./App.vue";
import router from "./router";
import { i18n } from "./i18n";

function enhanceIconButtons(root: ParentNode = document) {
  const buttons = [
    ...(root instanceof HTMLButtonElement && root.classList.contains("el-button") ? [root] : []),
    ...Array.from(root.querySelectorAll<HTMLButtonElement>(".el-button")),
  ];
  buttons.forEach((button) => {
    const icon = button.querySelector(".el-icon");
    const label = button.querySelector("span");
    const text = label?.textContent?.trim() || "";
    if (button.dataset.keepLabel === "true") {
      button.classList.remove("icon-button-compact");
      return;
    }
    if (!icon || !label || !text) return;
    button.classList.add("icon-button-compact");
    button.setAttribute("title", text);
    button.setAttribute("aria-label", text);
  });
}

function installIconButtonEnhancer() {
  const run = () => enhanceIconButtons();
  queueMicrotask(run);
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      mutation.addedNodes.forEach((node) => {
        if (node instanceof Element) enhanceIconButtons(node);
      });
      if (mutation.target instanceof Element) enhanceIconButtons(mutation.target);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
}

createApp(App).use(router).use(i18n).use(ElementPlus).mount("#app");
installIconButtonEnhancer();
