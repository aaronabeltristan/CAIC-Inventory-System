import { useEffect, useState } from "react";
import defaultAvatar from "../assets/default_user.jpg";

// Pictures already fetched in this tab: "userId:version" -> local URL
const cache = new Map();

// A user's profile picture, or the default one. `version` changes whenever the picture
// changes (null = no picture), so a new picture shows straight away everywhere.
export default function Avatar({ api, userId, version, className, alt = "" }) {
  const key = userId && version && api?.avatarBlob ? `${userId}:${version}` : null;
  // The picture fetched below, with the key it belongs to (so a stale one is never shown)
  const [fetched, setFetched] = useState(null);
  const url = key ? cache.get(key) || (fetched?.key === key ? fetched.url : null) : null;

  useEffect(() => {
    if (!key || cache.has(key)) return;
    let live = true;
    api.avatarBlob(userId, version).then(
      (blob) => {
        if (!blob) return;
        const local = URL.createObjectURL(blob);
        cache.set(key, local);
        if (live) setFetched({ key, url: local });
      },
      () => {}
    );
    return () => {
      live = false;
    };
  }, [api, key]); // eslint-disable-line react-hooks/exhaustive-deps

  return <img src={url || defaultAvatar} alt={alt} className={className} />;
}
