{
  description = "nix-cache benchmark: 12 small + 2 big real-world packages";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      names = [
        # 12 small things
        "hello" "cowsay" "jq" "ripgrep" "fd" "bat"
        "eza" "curl" "wget" "tree" "figlet" "btop"
        # 2 big things
        "firefox" "go"
      ];
    in {
      packages.${system} =
        builtins.listToAttrs (map (n: { name = n; value = pkgs.${n}; }) names);
    };
}
