{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/master";

  outputs = {nixpkgs, ...}: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};
    pythonPackages = p: with p; [tox];
  in {
    devShells.${system}.default = pkgs.mkShell {
      packages = [(pkgs.python312.withPackages pythonPackages)];
    };
  };
}

