{
  description = "Custom benchmark artifacts (not on nixos.org)";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let system = "x86_64-linux";
        pkgs = nixpkgs.legacyPackages.${system};
        mkArtifact = name: seed: pkgs.runCommand name {} ''
          mkdir -p $out
          ${pkgs.python3}/bin/python3 -c "
import random
random.seed(${toString seed})
with open('$out/data.bin', 'wb') as f:
    f.write(bytes(random.getrandbits(8) for _ in range(20*1024*1024)))
          "
          echo "${name}" > $out/README.txt
        '';
    in {
      packages.${system} = {
        custom-20m = mkArtifact "custom-benchmark-20m" 42;
        custom-20m-v2 = mkArtifact "custom-benchmark-20m-v2" 12345;
      };
    };
}
