// Crops a chosen photo to the middle square and shrinks it to size x size JPEG (about 30 KB),
// so uploads are small and every picture looks the same in its circle.
export async function squarePhoto(file, size = 256) {
  let source;
  try {
    source = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new Error("That file isn't a picture this browser can read. Try a JPEG or PNG.");
  }
  const side = Math.min(source.width, source.height);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; // transparent PNGs get a white background
  ctx.fillRect(0, 0, size, size);
  ctx.drawImage(source, (source.width - side) / 2, (source.height - side) / 2, side, side, 0, 0, size, size);
  source.close?.();
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Couldn't prepare the picture."))), "image/jpeg", 0.85)
  );
}
