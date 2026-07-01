import { createI18n } from "vue-i18n";
import zhCN from "./locales/zh-CN";
import enUS from "./locales/en-US";

export const supportedLocales = ["zh-CN", "en-US"] as const;
export type AppLocale = (typeof supportedLocales)[number];

const storageKey = "server-vps.locale";

function isAppLocale(value: string | null): value is AppLocale {
  return supportedLocales.includes(value as AppLocale);
}

const storedLocale = localStorage.getItem(storageKey);
const savedLocale = isAppLocale(storedLocale) ? storedLocale : null;
const initialLocale: AppLocale = savedLocale || "zh-CN";

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale,
  fallbackLocale: "zh-CN",
  messages: {
    "zh-CN": zhCN,
    "en-US": enUS,
  },
});

export function setAppLocale(locale: AppLocale) {
  i18n.global.locale.value = locale;
  localStorage.setItem(storageKey, locale);
  document.documentElement.lang = locale;
}

setAppLocale(initialLocale);
