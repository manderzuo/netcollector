import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const { SKILL_DIR, TMP_DIR, RUNTIME_PYTHON } = process.env;
const workspaceDir = "D:\\gpt\\douyinxiaohongshu";
const candidatePath = path.join(TMP_DIR, "candidate-manual-v1.pptx");
const finalPath = path.join(workspaceDir, "outputs", "多平台采集工作台-产品与操作说明-v1-更新稿.pptx");
const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL_DIR, "container_tools/artifact_tool_utils.mjs")).href);
const presentation = await PresentationFile.importPptx(await FileBlob.load(candidatePath));
const stagingDir = path.join(workspaceDir, ".ppt-build-v1");
await fs.mkdir(path.dirname(finalPath), { recursive: true });
const requirements = {
  explicitTotalSlideCount: 18,
  requiredNativeTableOwnerSlides: [],
  requiredNativeChartOwnerSlides: [],
  fontPolicy: { basis: "design", families: ["Arial"] },
};
const result = await finalizePresentation({
  ...requirements,
  workspaceDir,
  candidatePath,
  finalPath,
  pythonExecutable: RUNTIME_PYTHON,
  integrityValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs: ["--expected-slide-size-emu", "12192000,6858000", "--validate-bullet-geometry", "--validate-heading-fit"],
  requiredNativeTableOwnerSlides: [],
  fontPolicy: requirements.fontPolicy,
  verifyArtifactToolImport: true,
  receiptPath: path.join(stagingDir, "多平台采集工作台-产品与操作说明-v1-更新稿.validation.json"),
});
console.log(JSON.stringify({ finalPath, result }, null, 2));
