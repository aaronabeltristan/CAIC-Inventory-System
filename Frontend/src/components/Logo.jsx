import builtInLogo from "../assets/logo.webp";
import { logoUrl } from "../api/api";
import { useAppSettings } from "../hooks/useAppSettings";

// The company logo: the one set in Settings → Company, or the built-in one.
export default function Logo({ className }) {
  const { logo_version, company_name } = useAppSettings();
  return <img src={logoUrl(logo_version) || builtInLogo} alt={company_name} className={className} />;
}
