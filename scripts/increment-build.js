/**
 * Auto-increments the build number before every build.
 * Run automatically via "prebuild:mac" / "prebuild:win" npm hooks.
 */
const fs   = require("fs");
const path = require("path");

const buildFile = path.join(__dirname, "..", "build-number.json");
const data      = JSON.parse(fs.readFileSync(buildFile, "utf8"));

data.buildNumber = (data.buildNumber || 0) + 1;
fs.writeFileSync(buildFile, JSON.stringify(data, null, 2) + "\n");

console.log(`Build number → ${data.buildNumber}`);
