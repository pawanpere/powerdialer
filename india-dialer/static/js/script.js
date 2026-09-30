/* The script column. The call tree arrives in section 6. */
import { $ } from "./util.js";

export function wireScript() {
  $("step").innerHTML = '<p class="note">The call script loads here.</p>';
  $("step-nav").hidden = true;
}
export const scriptKey = () => false;
export const jumpTo = () => {};
export const objectionsOpen = () => false;
export const closeObjections = () => {};
