/**
 * The panel is one exported page served by the backend, possibly under a
 * secret prefix chosen at install time. Static export produces it; the
 * relativize script then rewrites the asset URLs in the HTML to be relative,
 * which is what lets the same build work at "/" and at "/<anything>/" without
 * being rebuilt. All in-app navigation is hash-based, so no other file is
 * ever requested by path.
 */
const nextConfig = {
  output: "export",
  reactStrictMode: true,
  images: { unoptimized: true },
  devIndicators: false,
};

export default nextConfig;
