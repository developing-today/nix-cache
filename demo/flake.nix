{
  description = "nix-cache demo: a content-addressed derivation to push/pull through the cache";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      packages.${system}.demo = pkgs.runCommand "nix-cache-ca-demo" {
        # Content-addressed: the output store path is derived from the
        # CONTENT, so any machine building this gets the identical path —
        # which is exactly what makes a shared binary cache work.
        __contentAddressed = true;
      } ''
        mkdir -p $out/share
        echo "nix-cache demo payload" > $out/share/payload.txt
        # Sleep makes substitution provable: a cache hit returns instantly,
        # a real rebuild takes ~10s.
        sleep 10
      '';
    };
}
