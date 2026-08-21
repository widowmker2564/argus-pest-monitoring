# Go2 DDS env (Orin). Source in each new terminal: source ~/setup_go2.sh
# Selects the dog link by IP so eth0/eth1 renames do not matter.
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><NetworkInterfaceAddress>192.168.123.18</NetworkInterfaceAddress></General></Domain></CycloneDDS>'